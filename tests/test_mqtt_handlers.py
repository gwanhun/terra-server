"""MQTT 핸들러 단위 테스트.

handlers.py 가 paho 의존이 없어서 Supabase mock 만으로 충분.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from backend.mqtt import handlers


DEVICE_TEXT = "terra-test01"
DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"

CAMERA_TEXT = "p4cam-aabbccdd"
CAMERA_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"


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


def test_device_cache_expires_after_ttl(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTL 지나면 다시 조회한다 — 브리지가 삭제된 기기의 옛 UUID 를 영원히 쓰던 문제."""
    _setup_device_lookup(fake_sb)
    clock = {"t": 1000.0}
    monkeypatch.setattr(handlers.time, "monotonic", lambda: clock["t"])

    assert handlers._cached_device_uuid(DEVICE_TEXT) == DEVICE_UUID
    assert fake_sb.table.call_count == 1

    clock["t"] += handlers.ENTITY_TTL_SEC - 1       # 아직 유효
    assert handlers._cached_device_uuid(DEVICE_TEXT) == DEVICE_UUID
    assert fake_sb.table.call_count == 1

    clock["t"] += 2                                  # 만료
    assert handlers._cached_device_uuid(DEVICE_TEXT) == DEVICE_UUID
    assert fake_sb.table.call_count == 2


def test_device_cache_negative_uses_short_ttl(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미페어링(None)은 짧게만 캐싱 — 페어링 직후 기기가 오래 무시되면 안 된다."""
    _setup_device_lookup(fake_sb, uuid=None)
    clock = {"t": 500.0}
    monkeypatch.setattr(handlers.time, "monotonic", lambda: clock["t"])

    assert handlers._cached_device_uuid("terra-new") is None
    assert fake_sb.table.call_count == 1

    clock["t"] += handlers.ENTITY_NEG_TTL_SEC + 1
    _setup_device_lookup(fake_sb)                    # 그 사이 페어링됨
    assert handlers._cached_device_uuid("terra-new") == DEVICE_UUID


def test_device_lookup_failure_not_cached(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """DB 장애를 '미페어링' 으로 굳히면 안 된다 — 복구 후 바로 다시 조회돼야."""
    fake_sb.table.side_effect = RuntimeError("supabase down")
    assert handlers._cached_device_uuid(DEVICE_TEXT) is None

    fake_sb.table.side_effect = None
    _setup_device_lookup(fake_sb)
    assert handlers._cached_device_uuid(DEVICE_TEXT) == DEVICE_UUID


def test_device_meta_returns_owner_name_enclosure(fake_sb: MagicMock) -> None:
    """푸시 이벤트가 쓰는 메타 조회 (commands 행에 없는 device_name/enclosure_id)."""
    meta = {"owner_id": "owner-1", "name": "거실 사육장", "enclosure_id": "enc-1"}
    chain = (
        fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    )
    chain.execute.return_value.data = [meta]

    assert handlers.device_meta(DEVICE_UUID) == meta
    assert handlers.device_meta(DEVICE_UUID) == meta   # 두 번째는 캐시
    assert fake_sb.table.call_count == 1


def test_device_meta_missing_returns_none(fake_sb: MagicMock) -> None:
    chain = (
        fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    )
    chain.execute.return_value.data = []
    assert handlers.device_meta("gone-uuid") is None


# ---------- handle_telemetry ----------


def test_handle_telemetry_inserts_full_payload(fake_sb: MagicMock) -> None:
    _setup_device_lookup(fake_sb)
    payload = {
        "ts": 1_748_000_000,
        "dht22_a": {"t": 25.3, "h": 62.1, "ok": True},
        "dht22_b": {"t": 24.8, "h": 60.5, "ok": True},
        "relay": "OFF",
        "fan": "ON",
        "fan2": "OFF",
        "heater": {"state": "OFF", "locked": False},
        "led": "ON",
        "led_brightness": 75,
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
    assert row["fan2"] == "OFF"
    assert row["heater_state"] == "OFF"
    assert row["heater_locked"] is False
    assert row["led"] == "ON"
    assert row["led_brightness"] == 75
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
    assert row["led"] is None            # led 미보고 시 None
    assert row["led_brightness"] is None
    assert row["t_b"] is None
    assert row["heater_state"] is None


def test_sensor_values_drops_readings_when_fault() -> None:
    """ok=false 면 t/h 를 버린다 — fault 값이 telemetry_30m 평균을 오염시켰던 버그."""
    assert handlers._sensor_values({"t": 0.0, "h": 0.0, "ok": False}) == (None, None, False)
    assert handlers._sensor_values({"t": -999, "h": 200, "ok": False}) == (None, None, False)


def test_sensor_values_keeps_readings_when_ok() -> None:
    assert handlers._sensor_values({"t": 25.3, "h": 62.1, "ok": True}) == (25.3, 62.1, True)


def test_sensor_values_missing_ok_treated_as_fault() -> None:
    """ok 키 자체가 없으면 신뢰할 수 없으므로 fault 취급 (기존 bool(get('ok', False)) 유지)."""
    assert handlers._sensor_values({"t": 25.3, "h": 62.1}) == (None, None, False)
    assert handlers._sensor_values({}) == (None, None, False)


def test_handle_telemetry_fault_sensor_stored_as_null(fake_sb: MagicMock) -> None:
    """A센서 fault + B센서 정상 → A만 NULL, B는 보존. ok 플래그는 그대로 남는다."""
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

    handlers.handle_telemetry(DEVICE_TEXT, {
        "ts": 1_748_000_000,
        "dht22_a": {"t": 0.0, "h": 0.0, "ok": False},    # fault — 값 무의미
        "dht22_b": {"t": 24.8, "h": 60.5, "ok": True},
    })

    row = inserts[0]
    assert row["t_a"] is None
    assert row["h_a"] is None
    assert row["a_ok"] is False
    assert row["t_b"] == 24.8
    assert row["h_b"] == 60.5
    assert row["b_ok"] is True


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


def _ack_table_factory(cmd_row: dict | None) -> object:
    """devices/commands mock. commands UPDATE 는 갱신된 행 전체를 돌려준다
    (postgrest return=representation) — 푸시 훅이 이 값을 쓴다."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
                [{"id": DEVICE_UUID, "owner_id": "owner-1", "name": "사육장 1",
                  "enclosure_id": "enc-1"}]
            )
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": DEVICE_UUID}]
            t.update.return_value = t._upd
        elif name == "commands":
            chain = MagicMock()
            chain.eq.return_value.eq.return_value.execute.return_value.data = (
                [cmd_row] if cmd_row else []
            )
            t.update.return_value = chain
        return t
    return _table


def test_handle_ack_enqueues_push_event_for_schedule(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """예약 명령 ACK 확정 → push_outbox 적재까지 이어진다."""
    from backend import push_events

    fake_sb.table.side_effect = _ack_table_factory({
        "id": "cmd-1", "device_id": DEVICE_UUID, "issued_by": "owner-1",
        "action": "fan_on", "result": "ok", "source": "schedule", "source_id": "sch-1",
    })

    captured: list[dict] = []
    monkeypatch.setenv("PUSH_EVENT_INGEST_URL", "https://example.test/ingest")
    monkeypatch.setenv("PUSH_EVENT_INGEST_SECRET", "s3cr3t")
    monkeypatch.setattr(push_events, "enqueue", lambda ev: captured.append(ev) or True)

    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-1", "result": "ok"})

    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == push_events.EVENT_STARTED
    assert ev["payload"]["device_name"] == "사육장 1"
    assert ev["payload"]["enclosure_id"] == "enc-1"
    assert ev["payload"]["schedule_id"] == "sch-1"


def test_handle_ack_manual_command_not_enqueued(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend import push_events

    fake_sb.table.side_effect = _ack_table_factory({
        "id": "cmd-1", "device_id": DEVICE_UUID, "issued_by": "owner-1",
        "action": "mist", "result": "ok", "source": "manual", "source_id": None,
    })
    captured: list[dict] = []
    monkeypatch.setenv("PUSH_EVENT_INGEST_URL", "https://example.test/ingest")
    monkeypatch.setenv("PUSH_EVENT_INGEST_SECRET", "s3cr3t")
    monkeypatch.setattr(push_events, "enqueue", lambda ev: captured.append(ev) or True)

    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-1", "result": "ok"})
    assert captured == []


def test_handle_ack_push_failure_does_not_break_ack(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """푸시 적재가 터져도 ack 처리(devices last_seen 갱신)는 계속돼야 한다."""
    from backend import push_events

    fake_sb.table.side_effect = _ack_table_factory({
        "id": "cmd-1", "device_id": DEVICE_UUID, "issued_by": "owner-1",
        "action": "fan_on", "result": "ok", "source": "schedule", "source_id": "sch-1",
    })
    monkeypatch.setenv("PUSH_EVENT_INGEST_URL", "https://example.test/ingest")
    monkeypatch.setenv("PUSH_EVENT_INGEST_SECRET", "s3cr3t")

    def _boom(_ev: dict) -> bool:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(push_events, "enqueue", _boom)

    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-1", "result": "ok"})

    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "devices" in table_calls          # last_seen 갱신까지 도달


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


# ---------- 카메라 케이스 (cameras 테이블 fallback) ----------


def _camera_table_factory(updates: list[dict]) -> "callable":
    """devices 미스 + cameras 히트 + cameras.update 캡처."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        elif name == "cameras":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": CAMERA_UUID}
            ]
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": CAMERA_UUID}]
            t.update.side_effect = lambda payload: updates.append(payload) or t._upd
        return t
    return _table


def test_handle_telemetry_camera_updates_last_seen_only(fake_sb: MagicMock) -> None:
    """카메라 telemetry: telemetry 테이블 INSERT 안 하고 cameras.last_seen/is_online 만 갱신."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_table_factory(updates)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1_748_000_000, "uptime_sec": 60, "wifi_rssi": -55})

    # telemetry INSERT 호출 없음 — 스키마 불일치
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "telemetry" not in table_calls

    # cameras UPDATE 한 번
    assert len(updates) == 1
    assert updates[0]["is_online"] is True
    assert "last_seen_at" in updates[0]


def test_handle_ack_camera_logs_msg_id_and_result(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """카메라 ack 는 commands 테이블이 없어 msg_id·result 를 info 로그로 남긴다(발행 로그와 대조)."""
    fake_sb.table.side_effect = _camera_table_factory([])
    with caplog.at_level(logging.INFO, logger="backend.mqtt.handlers"):
        handlers.handle_ack(CAMERA_TEXT, {"msg_id": "m-reboot-1", "result": "rejected_unknown_action"})
    line = next(r.getMessage() for r in caplog.records if "camera ack" in r.getMessage())
    assert "msg_id=m-reboot-1" in line and "result=rejected_unknown_action" in line


def test_handle_ack_camera_updates_last_seen_only(fake_sb: MagicMock) -> None:
    """카메라 ack: commands 매칭 안 하고 cameras.last_seen 만 갱신."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_table_factory(updates)

    handlers.handle_ack(CAMERA_TEXT, {"action": "webrtc_answer", "session_id": "s-1", "sdp": "..."})

    # commands 테이블 안 건드림
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "commands" not in table_calls

    # cameras UPDATE 한 번
    assert len(updates) == 1
    assert updates[0]["is_online"] is True


def test_resolve_entity_unknown_returns_none(fake_sb: MagicMock) -> None:
    """devices/cameras 둘 다 미스면 (None, None)."""
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value.data = []
    assert handlers._resolve_entity("ghost-xyz") == (None, None)


# ---------- 카메라 rotate_180 / capabilities 동기화 (2026-09-08) ----------


def _camera_state_table_factory(
    updates: list[dict], *, rotate_180: bool = False, capabilities: dict | None = None,
    hw_id: str | None = None, firmware_ver: str | None = None,
) -> "callable":
    """_camera_table_factory + cameras.select('rotate_180, capabilities, hw_id, firmware_ver') 응답."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        elif name == "cameras":
            def _select(cols: str = "id") -> MagicMock:
                sel = MagicMock()
                if "rotate_180" in cols:
                    data = [{"rotate_180": rotate_180, "capabilities": capabilities,
                             "hw_id": hw_id, "firmware_ver": firmware_ver}]
                else:
                    data = [{"id": CAMERA_UUID}]
                sel.eq.return_value.limit.return_value.execute.return_value.data = data
                return sel
            t.select.side_effect = _select
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": CAMERA_UUID}]
            t.update.side_effect = lambda payload: updates.append(payload) or t._upd
        return t
    return _table


@pytest.fixture
def published() -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    handlers.set_command_publisher(lambda cid, payload: calls.append((cid, payload)) or True)
    yield calls
    handlers.set_command_publisher(None)


def test_camera_telemetry_legacy_payload_unchanged(
    fake_sb: MagicMock, published: list
) -> None:
    """구 펌웨어(rotate_180/capabilities 없음): last_seen 만, 발행 없음, 상태 SELECT 도 없음."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, rotate_180=True)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1_748_000_000, "uptime_sec": 60, "free_heap": 1})

    assert len(updates) == 1
    assert set(updates[0]) == {"last_seen_at", "is_online"}
    assert published == []


def test_camera_telemetry_stores_capabilities_when_changed(
    fake_sb: MagicMock, published: list
) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, capabilities=None)

    handlers.handle_telemetry(
        CAMERA_TEXT, {"ts": 1, "rotate_180": False, "capabilities": {"rotate_180": True}}
    )

    assert updates[0]["capabilities"] == {"rotate_180": True}
    assert published == []  # DB rotate_180=False == 보고 False → 발행 없음

    # 두 번째 보고(같은 capabilities): 캐시 반영돼 UPDATE 에서 빠짐 (Realtime 잡음 방지)
    handlers.handle_telemetry(
        CAMERA_TEXT, {"ts": 2, "rotate_180": False, "capabilities": {"rotate_180": True}}
    )
    assert "capabilities" not in updates[1]


def test_camera_telemetry_updates_firmware_ver_when_changed(
    fake_sb: MagicMock, published: list
) -> None:
    """heartbeat `fw`(2026-09-28) 가 DB firmware_ver 와 다르면 갱신 — 리플래시 여부 판별.
    같은 값은 UPDATE 에서 뺀다(캐시), 구 펌웨어(fw 없음)는 건드리지 않는다."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, firmware_ver="fb2-p4 0.1.0")

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "fw": "fb2-p4 0.2.0-20260928"})
    assert updates[0]["firmware_ver"] == "fb2-p4 0.2.0-20260928"

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "fw": "fb2-p4 0.2.0-20260928"})
    assert "firmware_ver" not in updates[1]

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 3})
    assert "firmware_ver" not in updates[2]
    assert published == []


def test_camera_telemetry_backfills_hw_id_once(
    fake_sb: MagicMock, published: list
) -> None:
    """구 펌웨어로 등록돼 hw_id 가 비어 있던 행을, 새 펌웨어 하트비트가 한 번 채운다."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, hw_id=None)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "hw_id": "30EDA0E22E80"})
    # heartbeat 와 별도 UPDATE 로 쓴다 — 충돌이 last_seen 을 끌어내리지 않게 (2026-09-23)
    assert "hw_id" not in updates[0]
    assert updates[1] == {"hw_id": "30EDA0E22E80"}

    # 두 번째 하트비트: 캐시에 반영돼 다시 쓰지 않는다 (15초마다 쓰지 않는다)
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "hw_id": "30EDA0E22E80"})
    assert len(updates) == 3 and "hw_id" not in updates[2]


def test_camera_telemetry_keeps_existing_hw_id(
    fake_sb: MagicMock, published: list
) -> None:
    """이미 저장된 hw_id 는 다시 쓰지 않는다 (Realtime UPDATE 잡음 방지)."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, hw_id="30EDA0E22E80")

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "hw_id": "30EDA0E22E80"})
    assert "hw_id" not in updates[0]


def test_camera_telemetry_without_hw_id_does_not_write_it(
    fake_sb: MagicMock, published: list
) -> None:
    """구 펌웨어(hw_id 미보고)는 기존 동작 그대로 — hw_id 를 건드리지 않는다."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, hw_id=None)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1})
    assert "hw_id" not in updates[0]


def _dup_error() -> APIError:
    return APIError({"message": "duplicate key value violates unique constraint "
                                "\"cameras_owner_hw_id_uniq\"", "code": "23505",
                     "details": "", "hint": None})


def _camera_factory_hw_id_conflict(updates: list[dict]) -> "callable":
    """hw_id 만 담긴 UPDATE 에 23505 를 던지는 cameras 테이블 mock."""
    base = _camera_state_table_factory(updates, hw_id=None)

    def _table(name: str) -> MagicMock:
        t = base(name)
        if name == "cameras":
            def _update(payload: dict) -> MagicMock:
                updates.append(payload)
                if set(payload) == {"hw_id"}:
                    raise _dup_error()
                return t._upd
            t.update.side_effect = _update
        return t
    return _table


def test_camera_hw_id_conflict_does_not_break_heartbeat(
    fake_sb: MagicMock, published: list, caplog: pytest.LogCaptureFixture
) -> None:
    """hw_id UNIQUE 충돌이 나도 last_seen_at/is_online 은 매번 갱신되고, 재시도는 1회로 끝난다.

    2026-09-23 이전엔 같은 UPDATE 에 묶여 있어 충돌 한 번에 하트비트가 통째로 실패 →
    멀쩡한 카메라가 영구 오프라인으로 보였다. 이 테스트가 그 회귀를 막는다.
    """
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_factory_hw_id_conflict(updates)

    with caplog.at_level(logging.WARNING):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "hw_id": "30EDA0E22E80"})
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "hw_id": "30EDA0E22E80"})

    heartbeats = [u for u in updates if "last_seen_at" in u]
    hw_writes = [u for u in updates if set(u) == {"hw_id"}]
    assert len(heartbeats) == 2                          # 충돌과 무관하게 매번 갱신
    assert all(u["is_online"] is True for u in heartbeats)
    assert all("hw_id" not in u for u in heartbeats)     # 하트비트에 hw_id 가 섞이지 않음
    assert len(hw_writes) == 1                           # 충돌 후 재시도 안 함 (스팸 방지)
    assert "백필 충돌" in caplog.text


def _missing_column_error(column: str) -> APIError:
    return APIError({"message": f"Could not find the '{column}' column of 'cameras' in the schema cache",
                     "code": "PGRST204", "details": None, "hint": None})


def _camera_factory_rejecting(
    updates: list[dict], reject: "callable", *, rotate_180: bool = False,
    capabilities: dict | None = None, firmware_ver: str | None = None,
) -> "callable":
    """cameras UPDATE 마다 `reject(payload)` 가 돌려준 예외를 던지는 mock (None 이면 성공)."""
    base = _camera_state_table_factory(
        updates, rotate_180=rotate_180, capabilities=capabilities, firmware_ver=firmware_ver)

    def _table(name: str) -> MagicMock:
        t = base(name)
        if name == "cameras":
            def _update(payload: dict) -> MagicMock:
                updates.append(payload)
                exc = reject(payload)
                if exc is not None:
                    raise exc
                return t._upd
            t.update.side_effect = _update
        return t
    return _table


def _reject_column(column: str) -> "callable":
    """`column` 이 담긴 UPDATE 를 PGRST204 로 거부 (마이그레이션 누락 재현)."""
    return lambda payload: _missing_column_error(column) if column in payload else None


def test_camera_missing_optional_column_keeps_heartbeat(
    fake_sb: MagicMock, published: list, caplog: pytest.LogCaptureFixture
) -> None:
    """DB 에 없는 컬럼 하나만 빼고 다시 쓴다 — last_seen_at/is_online 과 나머지 선택 필드는 산다.

    2026-09-17~28: 펌웨어가 보낸 `img` 를 cameras.image_state 에 쓰는데 그 컬럼 마이그레이션이
    누락돼 heartbeat UPDATE 가 통째로 실패 → 녹화 중인 카메라가 전부 오프라인으로 보였다
    (9/21 hw_id 와 같은 구조).
    """
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_factory_rejecting(updates, _reject_column("image_state"))
    clips = {"rec": 3, "skip": 0, "up_fail": 0, "up_busy_s": -1}

    with caplog.at_level(logging.WARNING):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "clips": clips, "img": {"exp": 120}})

    assert len(updates) == 2
    assert "image_state" in updates[0]                   # 1차: 한 문장으로 시도
    assert set(updates[1]) == {"last_seen_at", "is_online", "clip_stats", "clip_stats_at"}
    assert updates[1]["is_online"] is True               # 2차: 없는 컬럼만 뺐다
    assert "image_state" in caplog.text                  # 어떤 컬럼이 문제였는지 로그에 남는다


def test_camera_missing_column_is_remembered(
    fake_sb: MagicMock, published: list, caplog: pytest.LogCaptureFixture
) -> None:
    """없는 컬럼은 기억해 다음 heartbeat 부터 미리 뺀다 — 15초×N대 실패 쓰기·경고 스팸 방지."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_factory_rejecting(updates, _reject_column("image_state"))

    with caplog.at_level(logging.WARNING):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "img": {"exp": 120}})
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "img": {"exp": 121}})

    assert len(updates) == 3                             # 1회차 2번(실패+재시도), 2회차 1번
    assert "image_state" not in updates[2]
    assert caplog.text.count("cameras.image_state") == 1  # 경고는 한 번만
    assert "Traceback" not in caplog.text                # 스택트레이스 스팸 없음


def test_camera_missing_column_is_retried_after_ttl(
    fake_sb: MagicMock, published: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """마이그레이션을 적용하면 TTL 뒤엔 재시작 없이 다시 쓴다."""
    updates: list[dict] = []
    missing = {"image_state"}
    fake_sb.table.side_effect = _camera_factory_rejecting(
        updates, lambda p: next((_missing_column_error(c) for c in missing if c in p), None))
    clock = [1000.0]
    monkeypatch.setattr(handlers.time, "monotonic", lambda: clock[0])

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "img": {"exp": 120}})
    missing.clear()                                      # 운영에서 마이그레이션 적용
    clock[0] += handlers.MISSING_COLUMN_TTL_SEC + 1
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "img": {"exp": 121}})

    assert "image_state" in updates[-1]


def test_camera_rejected_optional_value_falls_back_to_liveness(
    fake_sb: MagicMock, published: list, caplog: pytest.LogCaptureFixture
) -> None:
    """컬럼 누락이 아닌 쿼리 거부(타입·제약 위반 등)는 원인 필드를 모르니 필수 필드만 다시 쓴다."""
    updates: list[dict] = []
    bad_json = APIError({"message": "invalid input syntax for type json", "code": "22P02",
                         "details": None, "hint": None})
    fake_sb.table.side_effect = _camera_factory_rejecting(
        updates, lambda p: bad_json if "clip_stats" in p else None)

    with caplog.at_level(logging.ERROR):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "clips": {"rec": 1}})

    assert len(updates) == 2
    assert set(updates[1]) == {"last_seen_at", "is_online"}
    assert "clip_stats" in caplog.text


def test_camera_transient_error_is_not_retried(
    fake_sb: MagicMock, published: list, caplog: pytest.LogCaptureFixture
) -> None:
    """네트워크/타임아웃은 재시도해도 같은 이유로 실패하고 부하만 늘린다 — 다음 heartbeat 에 맡긴다."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_factory_rejecting(
        updates, lambda p: ConnectionError("supabase down"))

    with caplog.at_level(logging.ERROR):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "clips": {"rec": 1}})

    assert len(updates) == 1
    assert "cameras heartbeat UPDATE 실패" in caplog.text


def test_camera_liveness_retry_failure_does_not_abort_telemetry(
    fake_sb: MagicMock, published: list
) -> None:
    """필수 필드 재시도까지 실패해도 예외가 새지 않고 뒤 단계(rotate_180 동기화)는 돈다."""
    updates: list[dict] = []
    rejected = APIError({"message": "boom", "code": "XX000", "details": None, "hint": None})
    fake_sb.table.side_effect = _camera_factory_rejecting(
        updates, lambda p: rejected, rotate_180=True)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "clips": {"rec": 1}, "rotate_180": False})

    assert len(updates) == 2                             # 전체 1번 + 필수 필드 1번, 그 이상 없음
    assert set(updates[1]) == {"last_seen_at", "is_online"}
    assert len(published) == 1                           # DB(True) ≠ 보고(False) → 재발행


def test_camera_capabilities_cached_only_when_written(
    fake_sb: MagicMock, published: list
) -> None:
    """capabilities 는 실제로 DB 에 들어갔을 때만 캐시에 반영한다.

    실패했는데 캐시에 넣으면 다음 heartbeat 가 "같은 값"으로 보고 빼버려 영영 저장되지 않는다.
    """
    caps = {"rotate_180": True}
    updates: list[dict] = []
    bad = APIError({"message": "boom", "code": "XX000", "details": None, "hint": None})
    fake_sb.table.side_effect = _camera_factory_rejecting(
        updates, lambda p: bad if len(p) > 2 else None)    # 필수 필드만일 때만 성공

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "capabilities": caps})
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "capabilities": caps})

    assert sum("capabilities" in u for u in updates) == 2  # 실패했으니 다음에도 다시 시도


def test_camera_firmware_ver_cached_only_when_written(
    fake_sb: MagicMock, published: list
) -> None:
    """firmware_ver 도 capabilities 와 같은 규칙 — 필수 필드만 저장된 heartbeat 뒤엔 다시 시도한다.

    캐시에 먼저 넣으면 DB 는 구 버전인데 다음 heartbeat 가 "같은 값"으로 보고 빼버려,
    앱이 리플래시된 카메라를 구 펌웨어로 계속 본다(재시작 버튼 숨김).
    """
    fw = "fb2-p4 0.2.0-20260928"
    updates: list[dict] = []
    bad = APIError({"message": "boom", "code": "XX000", "details": None, "hint": None})
    fake_sb.table.side_effect = _camera_factory_rejecting(
        updates, lambda p: bad if len(p) > 2 else None, firmware_ver="fb2-p4 0.1.0")

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "fw": fw})
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "fw": fw})

    assert sum("firmware_ver" in u for u in updates) == 2  # 실패했으니 다음에도 다시 시도


def test_camera_capabilities_cached_when_saved_after_column_strip(
    fake_sb: MagicMock, published: list
) -> None:
    """없는 컬럼만 빼고 재시도해 capabilities 가 저장됐으면 캐시 반영 — 매 heartbeat 재기록 안 함."""
    caps = {"rotate_180": True}
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_factory_rejecting(updates, _reject_column("image_state"))

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "capabilities": caps, "img": {"exp": 1}})
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "capabilities": caps, "img": {"exp": 1}})

    assert "capabilities" in updates[1]                  # 재시도에서 저장됨
    assert "capabilities" not in updates[2]              # 같은 값이라 다음엔 안 씀


def test_camera_heartbeat_writes_once_when_columns_exist(fake_sb: MagicMock, published: list) -> None:
    """정상 경로는 UPDATE 1회 — 재시도 로직이 DB 쓰기·Realtime 이벤트를 늘리지 않는다."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates)

    handlers.handle_telemetry(
        CAMERA_TEXT, {"ts": 1, "clips": {"rec": 1}, "img": {"exp": 120}}
    )

    assert len(updates) == 1


def test_device_hw_id_conflict_does_not_break_heartbeat(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """기기도 동일 — 3초 주기 텔레메트리에서 hw_id 충돌이 last_seen 을 막지 않는다."""
    updates: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID}
            ]
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": DEVICE_UUID}]

            def _update(payload: dict) -> MagicMock:
                updates.append(payload)
                if set(payload) == {"hw_id"}:
                    raise _dup_error()
                return t._upd
            t.update.side_effect = _update
        elif name == "telemetry":
            t.insert.return_value.execute.return_value.data = [{"device_id": DEVICE_UUID}]
        return t

    fake_sb.table.side_effect = _table

    with caplog.at_level(logging.WARNING):
        handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "hw_id": "A0B7651C2908"})
        handlers.handle_telemetry(DEVICE_TEXT, {"ts": 2, "hw_id": "A0B7651C2908"})

    heartbeats = [u for u in updates if "last_seen_at" in u]
    hw_writes = [u for u in updates if set(u) == {"hw_id"}]
    assert len(heartbeats) == 2
    assert all("hw_id" not in u for u in heartbeats)
    assert len(hw_writes) == 1
    assert "백필 충돌" in caplog.text


def _device_table_factory(updates: list[dict], *, db_caps: dict | None) -> "callable":
    """devices.select → id+capabilities, devices.update 캡처, telemetry.insert 성공."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": DEVICE_UUID, "capabilities": db_caps}
            ]
            t._upd = MagicMock()
            t._upd.eq.return_value.execute.return_value.data = [{"id": DEVICE_UUID}]
            t.update.side_effect = lambda p: updates.append(p) or t._upd
        elif name == "telemetry":
            t.insert.return_value.execute.return_value.data = [{"device_id": DEVICE_UUID}]
        return t
    return _table


def test_device_telemetry_syncs_capabilities_when_changed(fake_sb: MagicMock) -> None:
    """펌웨어가 telemetry 로 보고한 capabilities 가 DB 와 다르면 1회 반영, 같은 값 반복은 생략.

    베타 기기는 페어링을 호출하지 않아 콘솔 기본값에 묶여 있었다 — 이 경로로 mist_max_ms 가 들어간다.
    """
    updates: list[dict] = []
    fake_sb.table.side_effect = _device_table_factory(
        updates, db_caps={"board": "mosfet", "led_dimmable": True})
    caps = {"board": "mosfet", "led_dimmable": True, "mist_max_ms": 10000}

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "capabilities": caps})
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 2, "capabilities": caps})

    cap_writes = [u for u in updates if "capabilities" in u]
    assert cap_writes == [{"capabilities": caps}]            # 첫 건만, 3초마다 쓰지 않는다
    assert len([u for u in updates if "last_seen_at" in u]) == 2


def test_device_telemetry_capabilities_same_as_db_not_written(fake_sb: MagicMock) -> None:
    updates: list[dict] = []
    same = {"board": "mosfet", "led_dimmable": True}
    fake_sb.table.side_effect = _device_table_factory(updates, db_caps=same)

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "capabilities": dict(same)})
    assert not [u for u in updates if "capabilities" in u]


def test_device_telemetry_saves_sys_state(fake_sb: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    """2026-09-28 펌웨어의 uptime/heap/reset/rssi 는 devices.sys_state 로(telemetry 행 X),
    uptime 이 줄면 재부팅 감지 경고. 카메라 clip_stats.sys 와 같은 키."""
    updates: list[dict] = []
    inserts: list[dict] = []
    base = _device_table_factory(updates, db_caps=None)

    def _table(name: str) -> MagicMock:
        t = base(name)
        if name == "telemetry":
            t.insert.side_effect = lambda p: inserts.append(p) or t.insert.return_value
        return t
    fake_sb.table.side_effect = _table

    handlers.handle_telemetry(DEVICE_TEXT, {
        "ts": 1, "uptime_sec": 300, "free_heap": 190000, "reset": "POWERON", "wifi_rssi": -40})
    with caplog.at_level(logging.WARNING, logger="backend.mqtt.handlers"):
        handlers.handle_telemetry(DEVICE_TEXT, {"ts": 2, "uptime_sec": 5, "reset": "SW:mqtt_reboot"})

    sys_writes = [u["sys_state"] for u in updates if "sys_state" in u]
    assert sys_writes == [
        {"uptime_s": 300, "reset": "POWERON", "heap": 190000, "rssi": -40},
        {"uptime_s": 5, "reset": "SW:mqtt_reboot"},
    ]
    assert any("재부팅 감지" in r.getMessage() and "SW:mqtt_reboot" in r.getMessage() for r in caplog.records)
    assert len(inserts) == 2
    assert all("sys_state" not in row and "uptime_sec" not in row for row in inserts)


def test_device_telemetry_saves_temp_offset(fake_sb: MagicMock) -> None:
    """2026-10-01 펌웨어의 temp_offset_c(지금 적용 중인 보정)는 devices.temp_offset_c 로(telemetry 행 X).
    구 펌웨어(키 없음)·범위 밖(±10 초과)·숫자 아님은 안 쓴다 — NULL 이 '미보고' 를 뜻하므로."""
    updates: list[dict] = []
    inserts: list[dict] = []
    base = _device_table_factory(updates, db_caps=None)

    def _table(name: str) -> MagicMock:
        t = base(name)
        if name == "telemetry":
            t.insert.side_effect = lambda p: inserts.append(p) or t.insert.return_value
        return t
    fake_sb.table.side_effect = _table

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "temp_offset_c": -1.5})
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 2, "temp_offset_c": 0})
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 3})                         # 구 펌웨어
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 4, "temp_offset_c": 42})    # 범위 밖
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 5, "temp_offset_c": "-1"})  # 숫자 아님
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 6, "temp_offset_c": True})  # bool 은 숫자 취급 X

    assert [u.get("temp_offset_c") for u in updates] == [-1.5, 0.0, None, None, None, None]
    assert all("temp_offset_c" not in row for row in inserts)


def _device_factory_rejecting(updates: list[dict], reject: "callable") -> "callable":
    """devices UPDATE 마다 `reject(payload)` 가 돌려준 예외를 던지는 mock (None 이면 성공)."""
    base = _device_table_factory(updates, db_caps=None)

    def _table(name: str) -> MagicMock:
        t = base(name)
        if name == "devices":
            def _update(payload: dict) -> MagicMock:
                updates.append(payload)
                exc = reject(payload)
                if exc is not None:
                    raise exc
                return t._upd
            t.update.side_effect = _update
        return t
    return _table


def test_device_missing_sys_state_column_keeps_liveness(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """devices.sys_state 컬럼이 없어도 last_seen_at/is_online 은 산다 — 카메라 image_state 사고
    (9/17~28, heartbeat UPDATE 통째 실패 → 전부 오프라인 표시)와 같은 구조를 기기에서도 막는다."""
    updates: list[dict] = []
    missing = APIError({"message": "Could not find the 'sys_state' column of 'devices' in the schema cache",
                        "code": "PGRST204", "details": None, "hint": None})
    fake_sb.table.side_effect = _device_factory_rejecting(
        updates, lambda p: missing if "sys_state" in p else None)

    with caplog.at_level(logging.WARNING):
        handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "uptime_sec": 300, "reset": "POWERON"})
        handlers.handle_telemetry(DEVICE_TEXT, {"ts": 2, "uptime_sec": 303, "reset": "POWERON"})

    assert set(updates[1]) == {"last_seen_at", "is_online"}    # 1회차 재시도: 없는 컬럼만 뺐다
    assert "sys_state" not in updates[2]                       # 2회차: 기억해서 미리 뺀다
    assert len(updates) == 3
    assert "devices.sys_state" in caplog.text


def test_device_rejected_sys_state_falls_back_to_liveness(fake_sb: MagicMock) -> None:
    """컬럼 누락이 아닌 거부(타입·제약 위반 등)도 필수 필드만 다시 써서 온라인 표시를 지킨다."""
    updates: list[dict] = []
    bad_json = APIError({"message": "invalid input syntax for type json", "code": "22P02",
                         "details": None, "hint": None})
    fake_sb.table.side_effect = _device_factory_rejecting(
        updates, lambda p: bad_json if "sys_state" in p else None)

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "uptime_sec": 300})

    assert len(updates) == 2
    assert set(updates[1]) == {"last_seen_at", "is_online"}


def test_device_telemetry_reboot_triggers_schedule_restore(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uptime 이 줄어든 첫 telemetry 에서 schedule_restore 가 1회 호출된다 (같은 부팅 후속은 X)."""
    from backend import schedule_restore
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(schedule_restore, "restore_after_reboot",
                        lambda sb, uuid, label, now_utc=None: calls.append((uuid, label)) or [])
    updates: list[dict] = []
    fake_sb.table.side_effect = _device_table_factory(updates, db_caps=None)

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1, "uptime_sec": 7200})
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 2, "uptime_sec": 4, "reset": "SW:mqtt_reboot"})
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 3, "uptime_sec": 7})

    assert calls == [(DEVICE_UUID, DEVICE_TEXT)]
    assert len([u for u in updates if "last_seen_at" in u]) == 3   # 복원과 무관하게 heartbeat 는 계속


def test_device_telemetry_old_firmware_no_sys_state(fake_sb: MagicMock) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _device_table_factory(updates, db_caps=None)
    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1})
    assert not [u for u in updates if "sys_state" in u]


def test_device_telemetry_without_capabilities_untouched(fake_sb: MagicMock) -> None:
    """구 펌웨어(키 없음)는 capabilities 를 건드리지 않는다 — 콘솔 기본값이 유지된다."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _device_table_factory(updates, db_caps=None)

    handlers.handle_telemetry(DEVICE_TEXT, {"ts": 1})
    assert not [u for u in updates if "capabilities" in u]


def test_camera_telemetry_skips_capabilities_when_same_as_db(
    fake_sb: MagicMock, published: list
) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(
        updates, capabilities={"rotate_180": True}
    )

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "capabilities": {"rotate_180": True}})

    assert "capabilities" not in updates[0]


def test_camera_telemetry_rotation_mismatch_republishes(
    fake_sb: MagicMock, published: list
) -> None:
    """DB rotate_180=True, 카메라 보고 False → set_rotation(True) 재발행."""
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, rotate_180=True)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "rotate_180": False})

    assert len(published) == 1
    cid, cmd = published[0]
    assert cid == CAMERA_TEXT
    assert cmd["action"] == "set_rotation"
    assert cmd["rotate_180"] is True
    assert cmd["ttl_sec"] == 60

    # 15초 뒤 같은 보고: 최소 재발행 간격(60초) 안이라 중복 발행 없음
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "rotate_180": False})
    assert len(published) == 1


def test_camera_telemetry_rotation_match_no_publish(
    fake_sb: MagicMock, published: list
) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, rotate_180=True)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "rotate_180": True})

    assert published == []


def test_camera_telemetry_mismatch_without_publisher_is_noop(fake_sb: MagicMock) -> None:
    """브리지 미등록(발행기 None)이면 경고만 남기고 예외 없이 last_seen 갱신은 유지."""
    handlers.set_command_publisher(None)
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates, rotate_180=True)

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "rotate_180": False})

    assert len(updates) == 1
    assert updates[0]["is_online"] is True


# ---------- 카메라 clip_stats (2026-09-16) ----------


def test_camera_telemetry_stores_clip_stats(fake_sb: MagicMock, published: list) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates)
    clips = {"rec": 3, "skip": 0, "skip_lock": 0, "up_ok": 3, "up_fail": 0,
             "sd_ok": 0, "sd_fail": 0, "sd_backlog": 0, "last_rec_s": 12, "up_busy_s": -1}

    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "clips": clips})

    assert updates[0]["clip_stats"] == clips
    assert "clip_stats_at" in updates[0]
    assert published == []


def test_camera_telemetry_clip_regression_logs_warning(
    fake_sb: MagicMock, published: list, caplog
) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates)
    base = {"rec": 3, "skip": 0, "up_fail": 0, "up_busy_s": -1}
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "clips": base})
    with caplog.at_level("WARNING"):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "clips": {**base, "skip": 2, "up_fail": 1}})
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 3, "clips": {**base, "up_busy_s": 400}})
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "슬롯 없음 스킵 +2" in msgs
    assert "업로드 실패 +1" in msgs
    assert "정체 의심" in msgs


def test_camera_telemetry_stores_image_state(fake_sb: MagicMock, published: list, caplog) -> None:
    updates: list[dict] = []
    fake_sb.table.side_effect = _camera_state_table_factory(updates)
    img = {"exp": 120, "luma": 80, "chroma": 5, "night": True, "ae_auto": True, "ae_frozen": True}
    with caplog.at_level("WARNING"):
        handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "img": img})
    assert updates[0]["image_state"] == img
    assert "AE 진동 동결" in " ".join(r.getMessage() for r in caplog.records)


# ---------- camera_logs (2026-10-01 펌웨어 errs) ----------


def test_camera_telemetry_stores_errs(fake_sb: MagicMock, published: list) -> None:
    """errs 배열 → camera_logs INSERT (줄당 200자 제한, prev/count 반영). 없으면 INSERT 안 함."""
    updates: list[dict] = []
    inner = _camera_state_table_factory(updates, rotate_180=True)
    logs_tbl = MagicMock()

    def _table(name: str) -> MagicMock:
        return logs_tbl if name == "camera_logs" else inner(name)

    fake_sb.table.side_effect = _table

    handlers.handle_telemetry(CAMERA_TEXT, {
        "ts": 1_748_000_000, "uptime_sec": 60,
        "errs": [
            {"up": 12, "n": 3, "prev": True, "m": "E (12000) terra_uploader: R2 PUT failed: ESP_FAIL"},
            {"up": 40, "n": 1, "prev": False, "m": "x" * 300},
            "garbage",
        ],
    })

    assert logs_tbl.insert.called, "camera_logs INSERT 없음"
    rows = logs_tbl.insert.call_args.args[0]
    assert len(rows) == 2
    assert rows[0]["prev_boot"] is True and rows[0]["count"] == 3 and rows[0]["uptime_s"] == 12
    assert rows[1]["prev_boot"] is False and rows[1]["count"] == 1 and len(rows[1]["msg"]) == 200
