"""MQTT 핸들러 단위 테스트.

handlers.py 가 paho 의존이 없어서 Supabase mock 만으로 충분.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
    updates: list[dict], *, rotate_180: bool = False, capabilities: dict | None = None
) -> "callable":
    """_camera_table_factory + cameras.select('rotate_180, capabilities') 응답."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        elif name == "cameras":
            def _select(cols: str = "id") -> MagicMock:
                sel = MagicMock()
                if "rotate_180" in cols:
                    data = [{"rotate_180": rotate_180, "capabilities": capabilities}]
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
