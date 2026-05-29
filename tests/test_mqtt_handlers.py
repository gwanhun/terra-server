"""MQTT 핸들러 단위 테스트.

handlers.py 가 paho 의존이 없어서 Supabase mock 만으로 충분.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.mqtt import handlers


DEVICE_TEXT = "terra-test01"
DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"


def _setup_device_lookup(
    fake_sb: MagicMock, uuid: str | None = DEVICE_UUID
) -> None:
    """devices.select.eq.limit.execute → [{id: uuid}] (또는 빈) 반환."""
    chain = (
        fake_sb.table.return_value
        .select.return_value
        .eq.return_value
        .limit.return_value
    )
    chain.execute.return_value.data = [{"id": uuid}] if uuid else []


@pytest.fixture(autouse=True)
def _reset_cache_and_sb(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock):
    """각 테스트마다 device_id 캐시 비우고 supabase mock 주입."""
    handlers.reset_device_cache()
    monkeypatch.setattr(handlers, "get_supabase_client", lambda: fake_sb)
    yield
    handlers.reset_device_cache()


# ---------- ts 정규화 ----------


def test_normalize_ts_epoch_seconds() -> None:
    iso = handlers._normalize_ts(1_748_000_000)
    assert iso.startswith("2025-")  # 1748000000 ≈ 2025-05


def test_normalize_ts_epoch_ms() -> None:
    iso = handlers._normalize_ts(1_748_000_000_000)
    assert iso.startswith("2025-")


def test_normalize_ts_monotonic_falls_back_to_now() -> None:
    iso = handlers._normalize_ts(123_456)
    # 현재 연도로 fallback (2026)
    assert iso.startswith("20")


def test_normalize_ts_none_falls_back_to_now() -> None:
    iso = handlers._normalize_ts(None)
    assert iso.startswith("20")


# ---------- device_id 캐시 ----------


def test_device_cache_hit_after_first_lookup(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    assert handlers._cached_device_uuid(DEVICE_TEXT) == DEVICE_UUID
    # 두 번째 호출은 DB 안 감 (캐시)
    assert handlers._cached_device_uuid(DEVICE_TEXT) == DEVICE_UUID
    # devices select 는 1번만 호출됐어야 함
    assert fake_sb.table.call_count == 1


def test_device_cache_miss_returns_none(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb, uuid=None)
    assert handlers._cached_device_uuid("unknown-xyz") is None


# ---------- handle_telemetry ----------


def test_handle_telemetry_inserts_full_payload(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    payload = {
        "ts": 1_748_000_000,
        "dht22_a": {"t": 25.3, "h": 62.1, "ok": True},
        "dht22_b": {"t": 24.8, "h": 60.5, "ok": True},
        "relay": "OFF",
        "fan": "ON",
        "heater": {"state": "OFF", "locked": False},
    }
    # telemetry.insert + devices.update 두 호출 — table() 호출별 분리
    inserts: list[dict] = []
    updates: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
            t.update.side_effect = lambda payload: updates.append(payload) or t._upd
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": DEVICE_UUID}]
        elif name == "telemetry":
            t.insert.side_effect = lambda payload: inserts.append(payload) or t._ins
            t._ins = MagicMock()
            t._ins.execute.return_value.data = [{"device_id": DEVICE_UUID}]
        return t

    fake_sb.table.side_effect = _table

    handlers.handle_telemetry(DEVICE_TEXT, payload)

    assert len(inserts) == 1
    row = inserts[0]
    assert row["device_id"] == DEVICE_UUID
    assert row["t_a"] == 25.3
    assert row["h_a"] == 62.1
    assert row["a_ok"] is True
    assert row["t_b"] == 24.8
    assert row["relay"] == "OFF"
    assert row["fan"] == "ON"
    assert row["heater_state"] == "OFF"
    assert row["heater_locked"] is False
    assert row["ts"].startswith("2025-")

    # devices 도 last_seen_at 갱신
    assert len(updates) == 1
    assert updates[0]["is_online"] is True
    assert "last_seen_at" in updates[0]


def test_handle_telemetry_unknown_device_skipped(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb, uuid=None)
    handlers.handle_telemetry("unknown-device", {"ts": 1_748_000_000})
    # devices 만 조회, telemetry insert 호출 없음
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "telemetry" not in table_calls


def test_handle_telemetry_handles_missing_sensors(fake_sb: MagicMock) -> None:
    """dht22_a/b 누락이면 t/h 가 None 이어야 함 (a_ok=False)."""
    _setup_device_lookup(fake_sb)

    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": DEVICE_UUID}]
            t.update.return_value = t._upd
        elif name == "telemetry":
            t.insert.side_effect = lambda payload: inserts.append(payload) or t._ins
            t._ins = MagicMock()
            t._ins.execute.return_value.data = [{}]
        return t

    fake_sb.table.side_effect = _table

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1_748_000_000})

    row = inserts[0]
    assert row["t_a"] is None
    assert row["a_ok"] is False
    assert row["t_b"] is None
    assert row["heater_state"] is None


def test_handle_telemetry_duplicate_pk_swallowed(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """동일 (device_id, ts) PK 충돌은 INFO/DEBUG 로 삼키고 raise X."""
    _setup_device_lookup(fake_sb)

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
        elif name == "telemetry":
            t.insert.return_value.execute.side_effect = Exception(
                "duplicate key value violates unique constraint (code 23505)"
            )
        return t

    fake_sb.table.side_effect = _table

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1_748_000_000})
    # 예외 안 던지면 통과


# ---------- handle_ack ----------


def test_handle_ack_updates_command(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    updates: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": DEVICE_UUID}]
            t.update.return_value = t._upd
        elif name == "commands":
            def _capture(payload: dict) -> MagicMock:
                updates.append(payload)
                chain = MagicMock()
                chain.eq.return_value.eq.return_value.execute.return_value.data = [
                    {"id": "cmd-1"}
                ]
                return chain
            t.update.side_effect = _capture
        return t

    fake_sb.table.side_effect = _table

    handlers.handle_ack(
        DEVICE_TEXT,
        {"msg_id": "cmd-1", "result": "ok", "state": {"heater": "ON"}},
    )

    assert len(updates) == 1
    assert updates[0]["status"] == "acked"
    assert updates[0]["result"] == "ok"
    assert "acked_at" in updates[0]


def test_handle_ack_missing_msg_id_skipped(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    handlers.handle_ack(DEVICE_TEXT, {"result": "ok"})
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "commands" not in table_calls


def test_handle_ack_unknown_device_skipped(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb, uuid=None)
    handlers.handle_ack("unknown-device", {"msg_id": "cmd-1", "result": "ok"})
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "commands" not in table_calls


# ---------- handle_alert ----------


def test_handle_alert_inserts(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
        elif name == "alerts":
            t.insert.side_effect = lambda payload: inserts.append(payload) or t._ins
            t._ins = MagicMock()
            t._ins.execute.return_value.data = [{"id": "a-1"}]
        return t

    fake_sb.table.side_effect = _table

    payload = {
        "kind": "temp_high",
        "severity": "warning",
        "message": "DHT22-A 45.2°C",
        "context": {"t_a": 45.2, "threshold": 45.0},
    }
    handlers.handle_alert(DEVICE_TEXT, payload)

    assert len(inserts) == 1
    row = inserts[0]
    assert row["device_id"] == DEVICE_UUID
    assert row["kind"] == "temp_high"
    assert row["severity"] == "warning"
    assert row["context"]["t_a"] == 45.2


def test_handle_alert_default_severity(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
        elif name == "alerts":
            t.insert.side_effect = lambda payload: inserts.append(payload) or t._ins
            t._ins = MagicMock()
            t._ins.execute.return_value.data = [{"id": "a-1"}]
        return t

    fake_sb.table.side_effect = _table

    handlers.handle_alert(DEVICE_TEXT, {"kind": "sensor_fault"})
    assert inserts[0]["severity"] == "warning"  # default


def test_handle_alert_missing_kind_skipped(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    handlers.handle_alert(DEVICE_TEXT, {"message": "no kind"})
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "alerts" not in table_calls
