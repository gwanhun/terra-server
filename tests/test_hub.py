"""Terra Hub(카메라+센서 통합 보드, specs/stage-k-unified-hub.md) — 서버 측 계약 테스트.

허브 1대 = cameras 행 + devices 행, 같은 텍스트 id(p4hub-…)·같은 MQTT 계정.
- /cameras/pair model=esp32-p4-hub → 두 행 생성 + cameras.device_id 링크 + 응답 device_uuid
- 브리지 telemetry: dht22_a 있으면 devices 경로, 없으면 cameras 경로
- 브리지 ack: commands 에 있으면 device ack, 없으면 camera ack(경고 없음)
- Mosquitto ACL: 같은 계정 블록을 두 번 쓰지 않는다
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.mqtt import handlers, registry
from tests.conftest import TEST_USER_ID

HUB_TEXT = "p4hub-aabbccdd"
HUB_CAM_UUID = "33333333-3333-3333-3333-cccccccccccc"
HUB_DEV_UUID = "44444444-4444-4444-4444-dddddddddddd"

HUB_SENSOR_PAYLOAD = {
    "ts": 1_748_000_000, "uptime_sec": 30, "free_heap": 1000, "reset": "POWERON", "wifi_rssi": -50,
    "dht22_a": {"t": 28.1, "h": 55.0, "ok": True}, "dht22_b": {"t": 0, "h": 0, "ok": False},
    "relay": "OFF", "fan": "OFF", "fan2": "OFF", "heater": {"state": "OFF", "locked": False},
    "led": "ON", "led_brightness": 80,
    "capabilities": {"board": "mosfet", "led_dimmable": True, "hub": True, "mist_max_ms": 30000},
    "temp_offset_c": -1.5,
}
HUB_HEARTBEAT_PAYLOAD = {
    "ts": 1_748_000_000, "uptime_sec": 30, "free_heap": 1000, "reset": "POWERON",
    "rotate_180": False, "capabilities": {"rotate_180": True},
}


# ---------- 페어링 ----------


def _hub_pair_tables(fake_sb: MagicMock, *, existing_cam: dict | None = None) -> dict[str, list]:
    """테이블별 mock. 반환값은 캡처 버킷 (cameras_insert/devices_insert/cameras_update/devices_update)."""
    cap: dict[str, list] = {
        "cameras_insert": [], "devices_insert": [], "cameras_update": [], "devices_update": [],
    }

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "cameras":
            # hw_id 조회 (select→eq→eq→is_→limit)
            t.select.return_value.eq.return_value.eq.return_value.is_.return_value \
                .limit.return_value.execute.return_value.data = [existing_cam] if existing_cam else []

            def _ins(payload):
                cap["cameras_insert"].append(payload)
                m = MagicMock()
                m.execute.return_value.data = [{
                    "id": HUB_CAM_UUID, "camera_id": payload["camera_id"], "name": payload["name"],
                    "enclosure_id": payload.get("enclosure_id"), "device_id": None,
                }]
                return m
            t.insert.side_effect = _ins

            def _upd(payload):
                cap["cameras_update"].append(payload)
                m = MagicMock()
                m.eq.return_value.execute.return_value.data = [{
                    "id": HUB_CAM_UUID, "camera_id": HUB_TEXT, "name": "거실 허브",
                    "enclosure_id": None, "device_id": (existing_cam or {}).get("device_id"),
                }]
                return m
            t.update.side_effect = _upd
        elif name == "devices":
            # device_id 텍스트 조회 — 없음
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []

            def _ins(payload):
                cap["devices_insert"].append(payload)
                m = MagicMock()
                m.execute.return_value.data = [{"id": HUB_DEV_UUID, **payload}]
                return m
            t.insert.side_effect = _ins

            def _upd(payload):
                cap["devices_update"].append(payload)
                m = MagicMock()
                m.eq.return_value.execute.return_value.data = [{"id": HUB_DEV_UUID}]
                return m
            t.update.side_effect = _upd
        return t

    fake_sb.table.side_effect = _table
    return cap


def test_pair_hub_creates_camera_and_linked_device(app_client: TestClient, fake_sb: MagicMock) -> None:
    cap = _hub_pair_tables(fake_sb)

    res = app_client.post("/cameras/pair", json={
        "name": "거실 허브", "model": "esp32-p4-hub", "hw_id": "30EDA0E22E80",
        "firmware_ver": "terra-hub-p4 0.1.0",
        "capabilities": {"board": "mosfet", "led_dimmable": True, "mist_max_ms": 30000},
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["camera_id"].startswith("p4hub-")
    assert body["device_uuid"] == HUB_DEV_UUID

    # devices 행: 같은 텍스트 id, 같은 토큰 해시, 허브 플래그 + 펌웨어 능력
    assert len(cap["devices_insert"]) == 1
    dev = cap["devices_insert"][0]
    cam = cap["cameras_insert"][0]
    assert dev["device_id"] == cam["camera_id"]
    assert dev["token_hash"] == cam["token_hash"]
    assert dev["owner_id"] == TEST_USER_ID
    assert dev["name"] == "거실 허브"
    assert dev["hw_id"] == "30EDA0E22E80"
    assert dev["capabilities"]["hub"] is True
    assert dev["capabilities"]["mist_max_ms"] == 30000

    # cameras.device_id 링크
    assert {"device_id": HUB_DEV_UUID} in cap["cameras_update"]


def test_pair_hub_reuse_updates_linked_device_token(app_client: TestClient, fake_sb: MagicMock) -> None:
    """같은 보드 재페어링: cameras 행 재사용 + 링크된 devices 행의 토큰만 갱신(새 행 없음)."""
    cap = _hub_pair_tables(fake_sb, existing_cam={
        "id": HUB_CAM_UUID, "camera_id": HUB_TEXT, "device_id": HUB_DEV_UUID,
    })

    res = app_client.post("/cameras/pair", json={
        "name": "거실 허브", "model": "esp32-p4-hub", "hw_id": "30EDA0E22E80",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["reused"] is True
    assert body["camera_id"] == HUB_TEXT
    assert body["device_uuid"] == HUB_DEV_UUID

    assert cap["cameras_insert"] == []
    assert cap["devices_insert"] == []
    assert len(cap["devices_update"]) == 1
    patch_ = cap["devices_update"][0]
    assert patch_["token_hash"].startswith("$2")
    assert patch_["capabilities"]["hub"] is True


def test_pair_hub_on_board_registered_as_plain_camera_renames_to_p4hub(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """p4cam- 으로 등록된 보드를 허브 펌웨어로 리플래시: 행 재사용 + camera_id → p4hub-, devices 행 신규.
    p4cam- 그대로면 브리지가 devices 를 조회하지 않아 센서 telemetry 가 버려진다."""
    cap = _hub_pair_tables(fake_sb, existing_cam={
        "id": HUB_CAM_UUID, "camera_id": "p4cam-11223344", "device_id": None,
    })
    unregistered: list[str] = []
    monkeypatch.setattr("backend.routers.cameras.registry.unregister_device", lambda cid: unregistered.append(cid) or True)

    res = app_client.post("/cameras/pair", json={
        "name": "거실 허브", "model": "esp32-p4-hub", "hw_id": "30EDA0E22E80",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["reused"] is True
    assert body["camera_id"].startswith("p4hub-")
    assert unregistered == ["p4cam-11223344"]

    cam_patch = cap["cameras_update"][0]
    assert cam_patch["camera_id"] == body["camera_id"]
    # devices 행은 새 p4hub- 텍스트로 생성 → 브리지 _resolve_both 가 둘 다 찾는다
    assert cap["devices_insert"][0]["device_id"] == body["camera_id"]


def test_pair_plain_camera_does_not_touch_devices(app_client: TestClient, fake_sb: MagicMock) -> None:
    cap = _hub_pair_tables(fake_sb)
    res = app_client.post("/cameras/pair", json={"name": "거실 카메라", "model": "esp32-p4"})
    assert res.status_code == 201, res.text
    assert res.json()["device_uuid"] is None
    assert cap["devices_insert"] == [] and cap["devices_update"] == []
    table_calls = [c.args[0] for c in fake_sb.table.call_args_list]
    assert "devices" not in table_calls


# ---------- 브리지: telemetry 라우팅 ----------


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock):
    handlers.reset_device_cache()
    monkeypatch.setattr(handlers, "get_supabase_client", lambda: fake_sb)
    yield
    handlers.reset_device_cache()


def _hub_bridge_tables(fake_sb: MagicMock, writes: dict[str, list]) -> None:
    """devices·cameras 둘 다 히트. INSERT/UPDATE payload 를 테이블별로 캡처."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        uuid = {"devices": HUB_DEV_UUID, "cameras": HUB_CAM_UUID}.get(name)

        def _select(cols: str = "id") -> MagicMock:
            sel = MagicMock()
            if name == "cameras" and "rotate_180" in cols:
                data = [{"rotate_180": False, "capabilities": {"rotate_180": True},
                         "hw_id": "X", "firmware_ver": None}]
            elif name == "devices" and "capabilities" in cols:
                data = [{"capabilities": HUB_SENSOR_PAYLOAD["capabilities"]}]
            elif uuid:
                data = [{"id": uuid, "unlinked_at": None}]
            else:
                data = []
            sel.eq.return_value.limit.return_value.execute.return_value.data = data
            sel.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
            return sel
        t.select.side_effect = _select

        def _ins(payload):
            writes.setdefault(f"{name}_insert", []).append(payload)
            m = MagicMock(); m.execute.return_value.data = [payload]
            return m
        t.insert.side_effect = _ins

        def _upd(payload):
            writes.setdefault(f"{name}_update", []).append(payload)
            m = MagicMock()
            m.eq.return_value.execute.return_value.data = [{"id": uuid}] if uuid else []
            m.eq.return_value.eq.return_value.execute.return_value.data = (
                writes.get("_commands_match", []) if name == "commands" else [{"id": uuid}]
            )
            return m
        t.update.side_effect = _upd
        return t
    fake_sb.table.side_effect = _table


def test_resolve_both_hub_hits_both_tables(fake_sb: MagicMock) -> None:
    _hub_bridge_tables(fake_sb, {})
    assert handlers._resolve_both(HUB_TEXT) == (HUB_DEV_UUID, HUB_CAM_UUID)
    # 호환 래퍼는 device 우선
    assert handlers._resolve_entity(HUB_TEXT) == ("device", HUB_DEV_UUID)


def test_resolve_both_prefix_skips_irrelevant_table(fake_sb: MagicMock) -> None:
    """terra- 는 devices 만, p4cam- 는 cameras 만 조회 — 3초 telemetry 마다 불필요한 왕복 없음."""
    _hub_bridge_tables(fake_sb, {})
    handlers._resolve_both("terra-abc")
    assert [c.args[0] for c in fake_sb.table.call_args_list] == ["devices"]
    fake_sb.table.reset_mock()
    handlers._resolve_both("p4cam-abc")
    assert [c.args[0] for c in fake_sb.table.call_args_list] == ["cameras"]


def test_hub_sensor_payload_goes_to_devices(fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: dict[str, list] = {}
    _hub_bridge_tables(fake_sb, writes)
    monkeypatch.setattr("backend.alerts.evaluate_telemetry", lambda *_a, **_k: None)
    monkeypatch.setattr("backend.schedule_restore.maybe_restore", lambda *_a, **_k: None)

    handlers.handle_telemetry(HUB_TEXT, dict(HUB_SENSOR_PAYLOAD))

    rows = writes.get("telemetry_insert", [])
    assert len(rows) == 1
    assert rows[0]["device_id"] == HUB_DEV_UUID
    assert rows[0]["t_a"] == 28.1 and rows[0]["led_brightness"] == 80
    dev_upd = writes.get("devices_update", [])
    assert any(u.get("is_online") is True for u in dev_upd)
    assert any(u.get("temp_offset_c") == -1.5 for u in dev_upd)
    # 카메라 행은 센서 payload 로 건드리지 않음
    assert not writes.get("cameras_update")


def test_hub_heartbeat_goes_to_cameras(fake_sb: MagicMock) -> None:
    writes: dict[str, list] = {}
    _hub_bridge_tables(fake_sb, writes)

    handlers.handle_telemetry(HUB_TEXT, dict(HUB_HEARTBEAT_PAYLOAD))

    assert not writes.get("telemetry_insert")
    assert not writes.get("devices_update")
    cam_upd = writes.get("cameras_update", [])
    assert len(cam_upd) >= 1 and cam_upd[0]["is_online"] is True


# ---------- 브리지: ack 라우팅 ----------


def test_hub_ack_matching_command_is_device_ack(fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: dict[str, list] = {"_commands_match": [{"id": "cmd-1", "action": "mist", "source": "manual"}]}
    _hub_bridge_tables(fake_sb, writes)
    monkeypatch.setattr(handlers, "_enqueue_command_event", lambda *_a, **_k: None)

    handlers.handle_ack(HUB_TEXT, {"msg_id": "cmd-1", "result": "ok", "state": "MIST"})

    assert writes["commands_update"][0]["status"] == "acked"
    assert any(u.get("is_online") for u in writes.get("devices_update", []))
    assert not writes.get("cameras_update")


def test_hub_ack_unknown_msg_id_is_camera_ack_without_warning(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """reboot/set_rotation/webrtc 같은 카메라 명령의 ack 는 commands 에 없다 → camera 경로, 경고 X."""
    writes: dict[str, list] = {"_commands_match": []}
    _hub_bridge_tables(fake_sb, writes)

    with caplog.at_level(logging.INFO, logger="backend.mqtt.handlers"):
        handlers.handle_ack(HUB_TEXT, {"msg_id": "m-reboot", "result": "ok"})

    assert not any("매칭되는 command 없음" in r.getMessage() for r in caplog.records)
    assert any("camera ack" in r.getMessage() for r in caplog.records)
    assert writes.get("cameras_update")
    assert not writes.get("devices_update")


def test_plain_device_ack_unknown_msg_id_still_warns(
    fake_sb: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """회귀 방지: 순수 기기는 기존처럼 replay/foreign 경고."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
            [{"id": "dev-1", "unlinked_at": None}] if name == "devices" else []
        )
        t.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "dev-1"}]
        return t
    fake_sb.table.side_effect = _table

    with caplog.at_level(logging.WARNING, logger="backend.mqtt.handlers"):
        handlers.handle_ack("terra-plain", {"msg_id": "ghost", "result": "ok"})
    assert any("매칭되는 command 없음" in r.getMessage() for r in caplog.records)


# ---------- Mosquitto ACL ----------


def test_acl_hub_account_written_once(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock) -> None:
    monkeypatch.setattr(registry, "get_supabase_client", lambda: fake_sb)

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        data = {
            "devices": [{"device_id": "terra-aa"}, {"device_id": HUB_TEXT}],
            "cameras": [{"camera_id": "p4cam-cc"}, {"camera_id": HUB_TEXT}],
        }[name]
        t.select.return_value.execute.return_value.data = data
        return t
    fake_sb.table.side_effect = _table

    content = registry._build_acl_content()
    assert content.count(f"user {HUB_TEXT}\n") == 1
    # 허브 블록은 카메라 상위집합(motion_event 포함)
    assert f"topic write esp32/{HUB_TEXT}/motion_event" in content
    assert f"topic write esp32/{HUB_TEXT}/telemetry" in content
    assert "user terra-aa" in content and "user p4cam-cc" in content
