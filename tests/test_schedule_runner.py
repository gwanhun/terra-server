"""schedule_runner.run_once 단위 테스트.

Supabase fluent chain mock. due 예약 → schedules UPDATE(next_run_at advance) +
commands INSERT 를 검증.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend import schedule_runner

DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
OWNER_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _inject_sb(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock):
    monkeypatch.setattr(schedule_runner, "get_supabase_client", lambda: fake_sb)
    yield


def _due_row(**over) -> dict:
    row = {
        "id": "sch-1",
        "device_id": DEVICE_UUID,
        "owner_id": OWNER_UUID,
        "action": "mist",
        "payload": {"duration_ms": 2000},
        "kind": "daily",
        "time_of_day": "08:00",
        "days_of_week": None,
    }
    row.update(over)
    return row


def test_no_due_schedules(fake_sb: MagicMock) -> None:
    (
        fake_sb.table.return_value.select.return_value
        .eq.return_value.lte.return_value.order.return_value
        .limit.return_value.execute.return_value.data
    ) = []
    assert schedule_runner.run_once() == 0


def test_due_schedule_advances_and_inserts_command(fake_sb: MagicMock) -> None:
    row = _due_row()
    updates: list[dict] = []
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            # SELECT due
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [row]

            # UPDATE (advance) 캡처
            def _upd(payload: dict) -> MagicMock:
                updates.append(payload)
                chain = MagicMock()
                chain.eq.return_value.execute.return_value.data = [{"id": "sch-1"}]
                return chain
            t.update.side_effect = _upd
        elif name == "commands":
            def _ins(payload: dict) -> MagicMock:
                inserts.append(payload)
                chain = MagicMock()
                chain.execute.return_value.data = [{"id": "cmd-1"}]
                return chain
            t.insert.side_effect = _ins
        return t

    fake_sb.table.side_effect = _table

    fired = schedule_runner.run_once()
    assert fired == 1

    # 1) next_run_at advance + last_run_at 기록
    assert len(updates) == 1
    assert "next_run_at" in updates[0] and "last_run_at" in updates[0]

    # 2) commands INSERT — action/payload/issued_by/source 전달
    assert len(inserts) == 1
    cmd = inserts[0]
    assert cmd["device_id"] == DEVICE_UUID
    assert cmd["action"] == "mist"
    assert cmd["payload"] == {"duration_ms": 2000}
    assert cmd["issued_by"] == OWNER_UUID
    assert cmd["status"] == "pending"
    assert cmd["source"] == "schedule"     # 요청 5: 예약 발행 출처
    assert cmd["source_id"] == "sch-1"


def _fire_and_capture(fake_sb: MagicMock, row: dict) -> list[dict]:
    """예약 1건을 발화시키고 commands INSERT payload 목록을 돌려준다."""
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [row]
            chain = MagicMock()
            chain.eq.return_value.execute.return_value.data = [{"id": "sch-1"}]
            t.update.return_value = chain
        elif name == "commands":
            def _ins(payload: dict) -> MagicMock:
                inserts.append(payload)
                c = MagicMock()
                c.execute.return_value.data = [{"id": "cmd-1"}]
                return c
            t.insert.side_effect = _ins
        return t

    fake_sb.table.side_effect = _table
    assert schedule_runner.run_once() == 1
    return inserts


def test_led_schedule_carries_brightness(fake_sb: MagicMock) -> None:
    """앱 2026-09-16 §2: 예약 실행 시 commands.payload 에 brightness 가 그대로 실린다."""
    inserts = _fire_and_capture(
        fake_sb, _due_row(action="led_on", payload={"brightness": 70})
    )
    assert inserts[0]["action"] == "led_on"
    assert inserts[0]["payload"] == {"brightness": 70}


def test_fan2_schedule_carries_duration_ms(fake_sb: MagicMock) -> None:
    """앱 2026-09-16 §3: 냉각팬 단건 예약의 duration_ms 가 그대로 실린다."""
    inserts = _fire_and_capture(
        fake_sb, _due_row(action="fan2_on", payload={"duration_ms": 1_800_000})
    )
    assert inserts[0]["action"] == "fan2_on"
    assert inserts[0]["payload"] == {"duration_ms": 1_800_000}


def test_guard_skips_when_humidity_above(fake_sb: MagicMock) -> None:
    """skip 형 가드: 습도가 임계 초과면 발행 안 하고 skipped 감사만 남김."""
    row = _due_row(guard={"type": "skip_when_humidity_above", "value": 60, "enabled": True})
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [row]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "sch-1"}]
        elif name == "telemetry":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                {"t_a": 25.0, "h_a": 72.0, "ts": "2026-08-12T00:00:00+00:00"}
            ]
        elif name == "commands":
            def _ins(payload: dict) -> MagicMock:
                inserts.append(payload)
                chain = MagicMock()
                chain.execute.return_value.data = [{"id": "cmd-x"}]
                return chain
            t.insert.side_effect = _ins
        return t

    fake_sb.table.side_effect = _table

    assert schedule_runner.run_once() == 1
    # 발행(pending) 아님 → skipped 감사 1건, source=guard
    assert len(inserts) == 1
    assert inserts[0]["status"] == "skipped"
    assert inserts[0]["source"] == "guard"
    assert inserts[0]["source_id"] == "sch-1"
    assert "습도" in inserts[0]["reason"]


def test_guard_passes_fires_normally(fake_sb: MagicMock) -> None:
    """가드 조건 미충족(습도 낮음)이면 정상 발행(pending)."""
    row = _due_row(guard={"type": "skip_when_humidity_above", "value": 90, "enabled": True})
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [row]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "sch-1"}]
        elif name == "telemetry":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                {"t_a": 25.0, "h_a": 50.0, "ts": "2026-08-12T00:00:00+00:00"}
            ]
        elif name == "commands":
            def _ins(payload: dict) -> MagicMock:
                inserts.append(payload)
                chain = MagicMock()
                chain.execute.return_value.data = [{"id": "cmd-x"}]
                return chain
            t.insert.side_effect = _ins
        return t

    fake_sb.table.side_effect = _table

    assert schedule_runner.run_once() == 1
    assert len(inserts) == 1
    assert inserts[0]["status"] == "pending"
    assert inserts[0]["source"] == "schedule"


def test_fire_advances_before_insert_failure_isolated(fake_sb: MagicMock) -> None:
    """한 예약 발화 실패해도 run_once 는 예외 전파 없이 카운트만 반영."""
    bad = _due_row(id="sch-bad", time_of_day="nope")  # parse 실패 유발

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [bad]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "sch-bad"}]
        return t

    fake_sb.table.side_effect = _table
    # 예외 없이 0 발화 (파싱 실패로 _fire_one 예외 → 잡힘)
    assert schedule_runner.run_once() == 0


def test_guard_skip_returns_structured_info(fake_sb: MagicMock) -> None:
    """푸시 guard 필드 재료 — kind/metric/threshold/value 가 숫자 그대로 나온다."""
    (
        fake_sb.table.return_value.select.return_value.eq.return_value
        .order.return_value.limit.return_value.execute.return_value.data
    ) = [{"t_a": 33.5, "h_a": 40.0, "ts": "2026-09-16T00:00:00+00:00"}]
    skip = schedule_runner._evaluate_skip_guard(
        fake_sb, DEVICE_UUID, {"type": "skip_when_temp_above", "value": 30, "enabled": True}
    )
    assert skip is not None
    assert skip["kind"] == "skip_when_temp_above"
    assert skip["metric"] == "temperature"
    assert skip["threshold"] == 30
    assert skip["value"] == 33.5
    assert "온도" in skip["reason"]


def test_guard_skip_enqueues_skipped_event_when_enabled(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """감사 행 INSERT 후 device.action.skipped 적재 훅이 호출된다 (스위치 켠 상태)."""
    from backend import push_events
    from backend.mqtt import handlers

    monkeypatch.setenv("PUSH_EVENT_SKIPPED_ENABLED", "true")
    monkeypatch.setattr(handlers, "_cached_device_text", lambda u: "terra-a1")
    monkeypatch.setattr(handlers, "device_meta", lambda u: {"owner_id": OWNER_UUID, "name": "n"})
    captured: list[tuple] = []
    monkeypatch.setattr(
        push_events, "enqueue_skipped_event",
        lambda row, key, meta, guard: captured.append((row["id"], key, guard)) or True,
    )

    row = _due_row(guard={"type": "skip_when_humidity_above", "value": 60, "enabled": True})

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [row]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "sch-1"}]
        elif name == "telemetry":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                {"t_a": 25.0, "h_a": 72.0, "ts": "2026-08-12T00:00:00+00:00"}
            ]
        elif name == "commands":
            c = MagicMock(); c.execute.return_value.data = [{"id": "cmd-skip", "source_id": "sch-1"}]
            t.insert.return_value = c
        return t

    fake_sb.table.side_effect = _table
    assert schedule_runner.run_once() == 1
    assert captured == [("cmd-skip", "terra-a1", {
        "reason": captured[0][2]["reason"], "kind": "skip_when_humidity_above",
        "metric": "humidity", "threshold": 60, "value": 72.0,
    })]
