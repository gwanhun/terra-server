"""dispatcher 단위 테스트.

bridge 는 stub (publish_command 만 mock), Supabase 는 fluent chain mock.
TTL/캐시/UPDATE 분기 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.mqtt import dispatcher, handlers


DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
DEVICE_TEXT = "terra-aabbccdd"


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock):
    """캐시 비우고 supabase mock 주입 (dispatcher + handlers 둘 다)."""
    handlers.reset_device_cache()
    monkeypatch.setattr(handlers, "get_supabase_client", lambda: fake_sb)
    monkeypatch.setattr(dispatcher, "get_supabase_client", lambda: fake_sb)
    yield
    handlers.reset_device_cache()


@pytest.fixture
def fake_bridge() -> MagicMock:
    """publish_command 만 검증하는 stub."""
    b = MagicMock()
    b.publish_command.return_value = True
    return b


def _now_iso(offset_sec: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()


def _setup_devices_lookup(fake_sb: MagicMock, uuid: str, text: str | None) -> None:
    """handlers._cached_device_text 를 위한 devices select 셋업."""
    chain = (
        fake_sb.table.return_value
        .select.return_value
        .eq.return_value
        .limit.return_value
    )
    chain.execute.return_value.data = [{"device_id": text}] if text else []


# ---------- 빈 결과 ----------


def test_no_pending_commands(fake_sb: MagicMock, fake_bridge: MagicMock) -> None:
    chain = (
        fake_sb.table.return_value
        .select.return_value
        .eq.return_value
        .order.return_value
        .limit.return_value
    )
    chain.execute.return_value.data = []

    n = dispatcher.poll_and_dispatch(fake_bridge)
    assert n == 0
    fake_bridge.publish_command.assert_not_called()


# ---------- 정상 흐름 ----------


def _dispatch_with_payload(
    fake_sb: MagicMock, fake_bridge: MagicMock, action: str, payload: dict | None
) -> dict:
    """payload 를 실은 pending 명령 1건을 발행하고 publish 된 MQTT payload 를 돌려준다."""
    cmd = {
        "id": "cmd-1",
        "device_id": DEVICE_UUID,
        "action": action,
        "payload": payload,
        "issued_at": _now_iso(),
        "ttl_sec": 30,
    }

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]
            chain = MagicMock()
            chain.eq.return_value.execute.return_value.data = [{"id": "cmd-1"}]
            t.update.return_value = chain
        elif name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"device_id": DEVICE_TEXT}
            ]
        return t

    fake_sb.table.side_effect = _table
    assert dispatcher.poll_and_dispatch(fake_bridge) == 1
    return fake_bridge.publish_command.call_args.args[1]


def test_payload_args_are_merged(fake_sb: MagicMock, fake_bridge: MagicMock) -> None:
    """LED 밝기·자동 시간 같은 action 인자는 그대로 실려 나간다 (앱 2026-09-16 §1·§2)."""
    out = _dispatch_with_payload(
        fake_sb, fake_bridge, "led_on", {"brightness": 60, "duration_ms": 3_600_000}
    )
    assert out["action"] == "led_on"
    assert out["brightness"] == 60
    assert out["duration_ms"] == 3_600_000


def test_payload_cannot_override_reserved_keys(
    fake_sb: MagicMock, fake_bridge: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """payload 가 프로토콜 필드를 덮어쓰면 안 된다 — 엉뚱한 action 이 발행될 수 있었음."""
    out = _dispatch_with_payload(
        fake_sb,
        fake_bridge,
        "led_on",
        {
            "brightness": 60,
            "action": "heater_on",       # 공격/버그: 다른 명령으로 바꿔치기
            "msg_id": "forged",
            "ttl_sec": 99999,
            "issued_at": 0,
        },
    )
    assert out["action"] == "led_on"     # 원래 action 유지
    assert out["msg_id"] == "cmd-1"
    assert out["ttl_sec"] == 30
    assert out["issued_at"] != 0
    assert out["brightness"] == 60       # 정상 인자는 통과


def test_non_dict_payload_ignored(fake_sb: MagicMock, fake_bridge: MagicMock) -> None:
    out = _dispatch_with_payload(fake_sb, fake_bridge, "led_on", None)
    assert out["action"] == "led_on"
    assert "brightness" not in out


def test_pending_command_publishes_and_updates_sent(
    fake_sb: MagicMock, fake_bridge: MagicMock
) -> None:
    cmd = {
        "id": "cmd-1",
        "device_id": DEVICE_UUID,
        "action": "heater_toggle",
        "payload": None,
        "issued_at": _now_iso(),
        "ttl_sec": 30,
    }
    updates: list[tuple[str, dict]] = []  # (table_name, payload)

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            # SELECT pending
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]

            # UPDATE chain — 캡처
            def _capture(payload: dict) -> MagicMock:
                updates.append(("commands", payload))
                chain = MagicMock()
                chain.eq.return_value.execute.return_value.data = [{"id": "cmd-1"}]
                return chain
            t.update.side_effect = _capture
        elif name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"device_id": DEVICE_TEXT}
            ]
        return t

    fake_sb.table.side_effect = _table

    n = dispatcher.poll_and_dispatch(fake_bridge)
    assert n == 1

    # publish 호출 검증
    fake_bridge.publish_command.assert_called_once()
    pub_args = fake_bridge.publish_command.call_args
    assert pub_args.args[0] == DEVICE_TEXT  # device text
    payload = pub_args.args[1]
    assert payload["msg_id"] == "cmd-1"
    assert payload["action"] == "heater_toggle"
    assert payload["ttl_sec"] == 30
    assert "issued_at" in payload

    # status='sent' UPDATE 호출 검증
    assert ("commands", {"status": "sent"}) in updates


# ---------- TTL 만료 ----------


def test_expired_command_marked_expired_not_published(
    fake_sb: MagicMock, fake_bridge: MagicMock
) -> None:
    cmd = {
        "id": "cmd-stale",
        "device_id": DEVICE_UUID,
        "action": "fan_toggle",
        "payload": None,
        "issued_at": _now_iso(-60),  # 60초 전
        "ttl_sec": 10,                # 만료
    }
    updates: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]

            def _capture(payload: dict) -> MagicMock:
                updates.append(payload)
                chain = MagicMock()
                chain.eq.return_value.execute.return_value.data = [{"id": "cmd-stale"}]
                return chain
            t.update.side_effect = _capture
        return t

    fake_sb.table.side_effect = _table

    n = dispatcher.poll_and_dispatch(fake_bridge)
    assert n == 1

    # publish 안 됨
    fake_bridge.publish_command.assert_not_called()

    # status='expired' UPDATE
    assert {"status": "expired"} in updates


# ---------- 미존재 디바이스 ----------


def test_unknown_device_marked_rejected(
    fake_sb: MagicMock, fake_bridge: MagicMock
) -> None:
    cmd = {
        "id": "cmd-unknown",
        "device_id": "00000000-0000-0000-0000-000000000000",
        "action": "relay_toggle",
        "payload": None,
        "issued_at": _now_iso(),
        "ttl_sec": 30,
    }
    updates: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]

            def _capture(payload: dict) -> MagicMock:
                updates.append(payload)
                chain = MagicMock()
                chain.eq.return_value.execute.return_value.data = [{"id": "cmd-unknown"}]
                return chain
            t.update.side_effect = _capture
        elif name == "devices":
            # 빈 결과
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        return t

    fake_sb.table.side_effect = _table

    n = dispatcher.poll_and_dispatch(fake_bridge)
    assert n == 1

    fake_bridge.publish_command.assert_not_called()
    assert {"status": "rejected", "result": "unknown_device"} in updates


# ---------- publish 실패 시 status 유지 ----------


def test_publish_failure_leaves_pending(
    fake_sb: MagicMock, fake_bridge: MagicMock
) -> None:
    fake_bridge.publish_command.return_value = False  # 실패

    cmd = {
        "id": "cmd-retry",
        "device_id": DEVICE_UUID,
        "action": "led_on",
        "payload": None,
        "issued_at": _now_iso(),
        "ttl_sec": 30,
    }
    updates: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]

            def _capture(payload: dict) -> MagicMock:
                updates.append(payload)
                chain = MagicMock()
                chain.eq.return_value.execute.return_value.data = [{"id": "cmd-retry"}]
                return chain
            t.update.side_effect = _capture
        elif name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"device_id": DEVICE_TEXT}
            ]
        return t

    fake_sb.table.side_effect = _table

    n = dispatcher.poll_and_dispatch(fake_bridge)
    assert n == 1

    # publish 시도는 됐지만 결과 False
    fake_bridge.publish_command.assert_called_once()
    # UPDATE 호출 없음 — pending 유지 (다음 poll 에서 재시도)
    assert updates == []


# ---------- payload extra 머지 ----------


def test_payload_extra_merged_into_publish(
    fake_sb: MagicMock, fake_bridge: MagicMock
) -> None:
    """commands.payload 의 추가 필드가 MQTT publish payload 에 머지되는지."""
    cmd = {
        "id": "cmd-rotate",
        "device_id": DEVICE_UUID,
        "action": "token_rotate",
        "payload": {"new_token": "abc123"},
        "issued_at": _now_iso(),
        "ttl_sec": 60,
    }

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "cmd-rotate"}]
        elif name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"device_id": DEVICE_TEXT}
            ]
        return t

    fake_sb.table.side_effect = _table

    dispatcher.poll_and_dispatch(fake_bridge)

    pub_payload = fake_bridge.publish_command.call_args.args[1]
    assert pub_payload["action"] == "token_rotate"
    assert pub_payload["new_token"] == "abc123"  # extra merged


# ---------- NULL ttl_sec 처리 ----------


def test_null_ttl_uses_default(
    fake_sb: MagicMock, fake_bridge: MagicMock
) -> None:
    cmd = {
        "id": "cmd-default-ttl",
        "device_id": DEVICE_UUID,
        "action": "fan_toggle",
        "payload": None,
        "issued_at": _now_iso(),
        "ttl_sec": None,
    }

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "commands":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [cmd]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "cmd-default-ttl"}]
        elif name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"device_id": DEVICE_TEXT}
            ]
        return t

    fake_sb.table.side_effect = _table

    dispatcher.poll_and_dispatch(fake_bridge)

    payload = fake_bridge.publish_command.call_args.args[1]
    assert payload["ttl_sec"] == dispatcher.DEFAULT_TTL_SEC
