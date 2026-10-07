"""MQTT 핸들러의 OTA 분기 — ack 라우팅(ota 블록·첫 ack·무관 ack)과 heartbeat fw 판정 트리거."""

from __future__ import annotations

from typing import Any

import pytest

from backend import ota_service
from backend.mqtt import handlers
from tests.fake_sb import FakeSB

DEVICE_TEXT = "terra-test01"
DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
CAMERA_TEXT = "p4cam-aabbccdd"
CAMERA_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"


@pytest.fixture
def sb(monkeypatch: pytest.MonkeyPatch) -> FakeSB:
    fake = FakeSB(
        devices=[{"id": DEVICE_UUID, "device_id": DEVICE_TEXT, "owner_id": "o", "firmware_ver": None,
                  "unlinked_at": None}],
        cameras=[{"id": CAMERA_UUID, "camera_id": CAMERA_TEXT, "owner_id": "o", "rotate_180": False,
                  "capabilities": None, "hw_id": None, "firmware_ver": "fb2-p4 0.2.1-20261006",
                  "unlinked_at": None}],
    )
    handlers.reset_device_cache()
    handlers._rebooted_recent.clear()
    handlers._device_fw_cache.clear()
    handlers._uptime_prev.clear()
    monkeypatch.setattr(handlers, "get_supabase_client", lambda: fake)
    monkeypatch.setattr(
        handlers, "_resolve_entity",
        lambda text: ("camera", CAMERA_UUID) if text == CAMERA_TEXT else ("device", DEVICE_UUID))
    from backend import alerts, schedule_restore
    monkeypatch.setattr(alerts, "evaluate_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(schedule_restore, "maybe_restore", lambda *a, **k: None)
    return fake


@pytest.fixture
def ack_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, Any]]:
    calls: list[tuple[str, Any, Any]] = []
    monkeypatch.setattr(ota_service, "apply_ack",
                        lambda sb, msg_id, result, ota: calls.append((msg_id, result, ota)))
    return calls


@pytest.fixture
def hb_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ota_service, "apply_heartbeat_fw",
                        lambda sb, uuid, fw: calls.append((uuid, fw)))
    return calls


# ---------- 기기 ack ----------

def test_device_progress_ack_skips_commands_and_routes_to_ota(sb: FakeSB, ack_calls: list) -> None:
    sb.rows("commands").append({"id": "cmd-1", "device_id": DEVICE_UUID, "action": "ota_prepare",
                                "status": "acked", "result": "ok"})
    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-1", "result": "ok",
                                      "ota": {"job_id": "j", "phase": "downloading", "pct": 30}})
    assert ack_calls == [("cmd-1", "ok", {"job_id": "j", "phase": "downloading", "pct": 30})]
    cmd = sb.one("commands", id="cmd-1")
    assert cmd["status"] == "acked" and "acked_at" not in cmd   # 두 번째 ack 가 commands 를 덮지 않음
    assert sb.one("devices", id=DEVICE_UUID)["is_online"] is True


def test_device_first_ota_ack_updates_command_then_routes(sb: FakeSB, ack_calls: list) -> None:
    sb.rows("commands").append({"id": "cmd-1", "device_id": DEVICE_UUID, "action": "ota_prepare",
                                "status": "sent", "source": "manual"})
    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-1", "result": "ok", "state": "OTA_PREPARE"})
    assert sb.one("commands", id="cmd-1")["status"] == "acked"
    assert ack_calls == [("cmd-1", "ok", None)]


def test_device_old_firmware_rejection_routes(sb: FakeSB, ack_calls: list) -> None:
    sb.rows("commands").append({"id": "cmd-1", "device_id": DEVICE_UUID, "action": "ota_prepare",
                                "status": "sent", "source": "manual"})
    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-1", "result": "rejected_unknown_action"})
    assert ack_calls == [("cmd-1", "rejected_unknown_action", None)]


def test_device_normal_ack_does_not_touch_ota(sb: FakeSB, ack_calls: list) -> None:
    sb.rows("commands").append({"id": "cmd-2", "device_id": DEVICE_UUID, "action": "led_on",
                                "status": "sent", "source": "manual"})
    handlers.handle_ack(DEVICE_TEXT, {"msg_id": "cmd-2", "result": "ok", "state": {"led": "ON"}})
    assert ack_calls == []
    assert sb.one("commands", id="cmd-2")["status"] == "acked"


# ---------- 카메라 ack ----------

def test_camera_ota_state_ack_routes(sb: FakeSB, ack_calls: list) -> None:
    handlers.handle_ack(CAMERA_TEXT, {"msg_id": "m1", "result": "ok", "state": "OTA_PREPARE"})
    assert ack_calls == [("m1", "ok", None)]
    assert sb.one("cameras", id=CAMERA_UUID)["is_online"] is True


def test_camera_ota_block_ack_routes(sb: FakeSB, ack_calls: list) -> None:
    handlers.handle_ack(CAMERA_TEXT, {"msg_id": "m1", "result": "ok", "ota": {"job_id": "j", "phase": "ready"}})
    assert ack_calls == [("m1", "ok", {"job_id": "j", "phase": "ready"})]


def test_camera_plain_ok_ack_not_routed(sb: FakeSB, ack_calls: list) -> None:
    """webrtc_answer 류 — 매 ack 마다 ota_jobs 를 보지 않는다."""
    handlers.handle_ack(CAMERA_TEXT, {"msg_id": "m2", "result": "ok", "action": "webrtc_answer"})
    assert ack_calls == []


def test_camera_rejected_ack_routed_for_old_firmware_detection(sb: FakeSB, ack_calls: list) -> None:
    handlers.handle_ack(CAMERA_TEXT, {"msg_id": "m3", "result": "rejected_unknown_action"})
    assert ack_calls == [("m3", "rejected_unknown_action", None)]


# ---------- heartbeat fw ----------

def test_device_fw_written_once_and_triggers_verify(sb: FakeSB, hb_calls: list) -> None:
    hb = {"ts": 1_748_000_000, "dht22_a": {"ok": True, "t": 30, "h": 50}, "uptime_sec": 100,
          "fw": "terra-fw 1.1.0"}
    handlers.handle_telemetry(DEVICE_TEXT, dict(hb))
    assert sb.one("devices", id=DEVICE_UUID)["firmware_ver"] == "terra-fw 1.1.0"
    assert hb_calls == [(DEVICE_UUID, "terra-fw 1.1.0")]

    handlers.handle_telemetry(DEVICE_TEXT, {**hb, "uptime_sec": 103})
    assert hb_calls == [(DEVICE_UUID, "terra-fw 1.1.0")]   # 같은 fw·재부팅 없음 → 조회 안 함


def test_device_reboot_with_same_fw_triggers_verify(sb: FakeSB, hb_calls: list) -> None:
    """롤백이면 fw 가 그대로라 '변경' 으로는 안 잡힌다 — uptime 리셋으로 잡는다."""
    hb = {"ts": 1_748_000_000, "dht22_a": {"ok": True, "t": 30, "h": 50}, "fw": "terra-fw 1.0.0"}
    handlers.handle_telemetry(DEVICE_TEXT, {**hb, "uptime_sec": 500})
    assert len(hb_calls) == 1          # 첫 보고(캐시 비어 있음)
    handlers.handle_telemetry(DEVICE_TEXT, {**hb, "uptime_sec": 503})
    assert len(hb_calls) == 1
    handlers.handle_telemetry(DEVICE_TEXT, {**hb, "uptime_sec": 12})   # 재부팅
    assert hb_calls[-1] == (DEVICE_UUID, "terra-fw 1.0.0") and len(hb_calls) == 2


def test_camera_fw_change_triggers_verify(sb: FakeSB, hb_calls: list) -> None:
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "uptime_sec": 40, "fw": "fb2-p4 0.3.0-20261010"})
    assert sb.one("cameras", id=CAMERA_UUID)["firmware_ver"] == "fb2-p4 0.3.0-20261010"
    assert hb_calls == [(CAMERA_UUID, "fb2-p4 0.3.0-20261010")]


def test_camera_same_fw_no_reboot_no_verify(sb: FakeSB, hb_calls: list) -> None:
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 1, "uptime_sec": 400, "fw": "fb2-p4 0.2.1-20261006"})
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 2, "uptime_sec": 415, "fw": "fb2-p4 0.2.1-20261006"})
    assert hb_calls == []
    handlers.handle_telemetry(CAMERA_TEXT, {"ts": 3, "uptime_sec": 20, "fw": "fb2-p4 0.2.1-20261006"})
    assert hb_calls == [(CAMERA_UUID, "fb2-p4 0.2.1-20261006")]
