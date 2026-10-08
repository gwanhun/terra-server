"""ota_service 단위 테스트 — 게이트, 작업 생성, ack/heartbeat 전이, 시한 스윕."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import ota_service as svc
from tests.fake_sb import FakeSB

CAM_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"
DEV_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
REL_CAM = "rel-cam-1"
REL_DEV = "rel-dev-1"


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _release(rid: str = REL_CAM, target: str = "camera_p4", version: str = "fb2-p4 0.3.0-20261010") -> dict:
    return {"id": rid, "target": target, "version": version, "r2_key": f"firmware/{target}/x.bin",
            "size_bytes": 1_800_000, "sha256": "a" * 64}


def _camera(**over) -> dict:
    base = {
        "id": CAM_UUID, "owner_id": "owner-1", "camera_id": "p4cam-aabbccdd", "unlinked_at": None,
        "firmware_ver": "fb2-p4 0.2.1-20261006", "capabilities": {"ota": True, "rotate_180": True},
        "clip_stats": {"up_busy_s": -1, "sys": {"uptime_s": 3600, "rssi": -55, "int_largest": 60_000}},
        "last_seen_at": _iso(10), "is_online": True, "live_session_id": None,
    }
    base.update(over)
    return base


def _device(**over) -> dict:
    base = {
        "id": DEV_UUID, "owner_id": "owner-1", "device_id": "terra-test01", "unlinked_at": None,
        "firmware_ver": "terra-fw 1.0.0", "capabilities": {"ota": True},
        "sys_state": {"uptime_s": 3600, "rssi": -60, "int_largest": 90_000},
        "last_seen_at": _iso(5), "is_online": True,
    }
    base.update(over)
    return base


# ---------- 게이트 ----------

def test_gate_passes_healthy_camera() -> None:
    assert svc.gate_reasons("camera", _camera()) == []


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"is_online": False}, "offline"),
        ({"last_seen_at": _iso(600)}, "offline"),
        ({"last_seen_at": None}, "never_seen"),
        ({"capabilities": {"rotate_180": True}}, "no_ota_capability"),
        ({"capabilities": None}, "no_ota_capability"),
        ({"clip_stats": {"up_busy_s": 12, "sys": {"uptime_s": 3600, "rssi": -55, "int_largest": 60_000}}}, "uploading"),
        ({"clip_stats": {"up_busy_s": -1, "sys": {"uptime_s": 3600, "rssi": -80, "int_largest": 60_000}}}, "weak_wifi"),
        ({"clip_stats": {"up_busy_s": -1, "sys": {"uptime_s": 3600, "rssi": -55, "int_largest": 6_000}}}, "low_internal_ram"),
        ({"clip_stats": {"up_busy_s": -1, "sys": {"uptime_s": 20, "rssi": -55, "int_largest": 60_000}}}, "just_booted"),
        ({"live_session_id": "sess-1"}, "live_active"),
    ],
)
def test_gate_reasons_camera(over: dict, expected: str) -> None:
    assert expected in svc.gate_reasons("camera", _camera(**over))


def test_gate_device_uses_sys_state() -> None:
    assert svc.gate_reasons("device", _device()) == []
    assert "weak_wifi" in svc.gate_reasons("device", _device(sys_state={"uptime_s": 999, "rssi": -90}))


# ---------- 작업 생성 ----------

def test_create_job_ok_returns_prepare_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_PUBLIC_BASE_URL", "https://api.test/")
    sb = FakeSB(firmware_releases=[_release()])
    job, release, payload = svc.create_job(
        sb, kind="camera", entity=_camera(), release_id=REL_CAM, issued_by="owner-1")
    assert job["status"] == "pending"
    assert job["prev_version"] == "fb2-p4 0.2.1-20261006"
    assert job["forced"] is False
    assert release["version"] == "fb2-p4 0.3.0-20261010"
    assert payload == {
        "job_id": job["id"], "version": "fb2-p4 0.3.0-20261010", "size": 1_800_000,
        "sha256": "a" * 64, "url": f"https://api.test/firmware/jobs/{job['id']}/bin",
    }
    assert sb.one("ota_jobs", id=job["id"])["target_uuid"] == CAM_UUID


def test_create_job_rejects_target_mismatch() -> None:
    sb = FakeSB(firmware_releases=[_release(REL_DEV, target="device_nano")])
    with pytest.raises(svc.OtaError) as ei:
        svc.create_job(sb, kind="camera", entity=_camera(), release_id=REL_DEV, issued_by=None)
    assert ei.value.status_code == 400


def test_create_job_rejects_retired_release() -> None:
    rel = {**_release(), "retired_at": _iso(60), "retired_reason": "prepare 스택 버그"}
    sb = FakeSB(firmware_releases=[rel])
    with pytest.raises(svc.OtaError) as ei:
        svc.create_job(sb, kind="camera", entity=_camera(), release_id=REL_CAM, issued_by=None, force=True)
    assert ei.value.status_code == 409
    assert "retired" in ei.value.detail and "프레페어" not in ei.value.detail
    assert sb.rows("ota_jobs") == []


def test_create_job_rejects_same_version() -> None:
    sb = FakeSB(firmware_releases=[_release(version="fb2-p4 0.2.1-20261006")])
    with pytest.raises(svc.OtaError, match="already on this version"):
        svc.create_job(sb, kind="camera", entity=_camera(), release_id=REL_CAM, issued_by=None)


def test_create_job_rejects_active_job() -> None:
    sb = FakeSB(firmware_releases=[_release()],
                ota_jobs=[{"id": "j0", "kind": "camera", "target_uuid": CAM_UUID, "release_id": REL_CAM,
                           "status": "downloading", "created_at": _iso(60)}])
    with pytest.raises(svc.OtaError, match="active job exists: j0"):
        svc.create_job(sb, kind="camera", entity=_camera(), release_id=REL_CAM, issued_by=None)


def test_create_job_terminal_jobs_do_not_block() -> None:
    sb = FakeSB(firmware_releases=[_release()],
                ota_jobs=[{"id": "j0", "kind": "camera", "target_uuid": CAM_UUID, "release_id": REL_CAM,
                           "status": "failed", "created_at": _iso(60)}])
    job, _, _ = svc.create_job(sb, kind="camera", entity=_camera(), release_id=REL_CAM, issued_by=None)
    assert job["status"] == "pending"


def test_create_job_gate_blocks_unless_forced() -> None:
    sb = FakeSB(firmware_releases=[_release()])
    cam = _camera(is_online=False)
    with pytest.raises(svc.OtaError, match="precheck failed: offline"):
        svc.create_job(sb, kind="camera", entity=cam, release_id=REL_CAM, issued_by=None)
    job, _, _ = svc.create_job(sb, kind="camera", entity=cam, release_id=REL_CAM, issued_by=None, force=True)
    assert job["forced"] is True


def test_create_job_release_not_found() -> None:
    with pytest.raises(svc.OtaError) as ei:
        svc.create_job(FakeSB(), kind="camera", entity=_camera(), release_id="nope", issued_by=None)
    assert ei.value.status_code == 404


# ---------- ack 전이 ----------

def _sb_with_job(status: str = "pending", **over) -> tuple[FakeSB, dict]:
    job = {"id": "job-1", "kind": "camera", "target_uuid": CAM_UUID, "release_id": REL_CAM,
           "status": status, "pct": 0, "prepare_msg_id": "msg-p", "apply_msg_id": None,
           "prev_version": "fb2-p4 0.2.1-20261006", "updated_at": _iso(0), "applied_at": None}
    job.update(over)
    return FakeSB(firmware_releases=[_release()], ota_jobs=[job]), job


def test_ack_ok_moves_pending_to_accepted() -> None:
    sb, _ = _sb_with_job()
    out = svc.apply_ack(sb, "msg-p", "ok", None)
    assert out["status"] == "accepted"


def test_ack_unknown_action_fails_as_old_firmware() -> None:
    sb, _ = _sb_with_job()
    out = svc.apply_ack(sb, "msg-p", "rejected_unknown_action", None)
    assert (out["status"], out["error"]) == ("failed", "old_firmware")
    assert out["finished_at"]


def test_ack_busy_fails_retryable() -> None:
    sb, _ = _sb_with_job()
    assert svc.apply_ack(sb, "msg-p", "busy", None)["error"] == "busy"


def test_ack_progress_and_ready() -> None:
    sb, _ = _sb_with_job("accepted")
    out = svc.apply_ack(sb, "msg-p", "ok", {"job_id": "job-1", "phase": "downloading", "pct": 42})
    assert (out["status"], out["pct"]) == ("downloading", 42)
    out = svc.apply_ack(sb, "msg-p", "ok", {"job_id": "job-1", "phase": "ready"})
    assert (out["status"], out["pct"]) == ("ready", 100)


def test_ack_failed_phase_records_error() -> None:
    sb, _ = _sb_with_job("downloading")
    out = svc.apply_ack(sb, "msg-p", "error", {"job_id": "job-1", "phase": "failed", "error": "tls"})
    assert (out["status"], out["error"]) == ("failed", "tls")


def test_ack_unrelated_msg_returns_none() -> None:
    sb, _ = _sb_with_job()
    assert svc.apply_ack(sb, "someone-else", "ok", None) is None


def test_ack_on_terminal_job_is_ignored() -> None:
    sb, _ = _sb_with_job("failed", error="busy")
    out = svc.apply_ack(sb, "msg-p", "ok", {"job_id": "job-1", "phase": "ready"})
    assert out["status"] == "failed"


def test_ack_job_id_mismatch_is_ignored() -> None:
    sb, _ = _sb_with_job("accepted")
    out = svc.apply_ack(sb, "msg-p", "ok", {"job_id": "other", "phase": "ready"})
    assert out["status"] == "accepted"


# ---------- heartbeat 판정 ----------

def test_heartbeat_new_version_verifies() -> None:
    sb, _ = _sb_with_job("applying", apply_msg_id="msg-a", applied_at=_iso(30))
    out = svc.apply_heartbeat_fw(sb, CAM_UUID, "fb2-p4 0.3.0-20261010")
    assert out["status"] == "verified"


def test_heartbeat_prev_version_means_rollback() -> None:
    sb, _ = _sb_with_job("applying", apply_msg_id="msg-a", applied_at=_iso(30))
    out = svc.apply_heartbeat_fw(sb, CAM_UUID, "fb2-p4 0.2.1-20261006")
    assert (out["status"], out["error"]) == ("rolled_back", "bootloader_rollback")


def test_heartbeat_without_applying_job_is_noop() -> None:
    sb, _ = _sb_with_job("ready")
    assert svc.apply_heartbeat_fw(sb, CAM_UUID, "fb2-p4 0.3.0-20261010") is None
    assert sb.one("ota_jobs", id="job-1")["status"] == "ready"


# ---------- 시한 스윕 ----------

def test_scan_times_out_stale_download() -> None:
    sb, _ = _sb_with_job("downloading", updated_at=_iso(svc.TIMEOUT_BY_STATUS_SEC["downloading"] + 5))
    assert svc.scan_once(sb) == {"timeout": 1, "failed": 0}
    assert sb.one("ota_jobs", id="job-1")["status"] == "timeout"


def test_scan_leaves_ready_alone_forever() -> None:
    sb, _ = _sb_with_job("ready", updated_at=_iso(10 * 24 * 3600))
    assert svc.scan_once(sb) == {"timeout": 0, "failed": 0}


def test_scan_applying_uses_applied_at() -> None:
    sb, _ = _sb_with_job("applying", applied_at=_iso(svc.TIMEOUT_BY_STATUS_SEC["applying"] + 1),
                         updated_at=_iso(0))
    assert svc.scan_once(sb)["timeout"] == 1


def test_scan_device_job_fails_when_command_expired() -> None:
    sb, _ = _sb_with_job("pending", kind="device", target_uuid=DEV_UUID, prepare_msg_id="cmd-1")
    sb.rows("commands").append({"id": "cmd-1", "status": "expired", "result": "expired"})
    assert svc.scan_once(sb) == {"timeout": 0, "failed": 1}
    assert sb.one("ota_jobs", id="job-1")["error"] == "expired"


def test_scan_device_unknown_action_maps_to_old_firmware() -> None:
    sb, _ = _sb_with_job("pending", kind="device", target_uuid=DEV_UUID, prepare_msg_id="cmd-1")
    sb.rows("commands").append({"id": "cmd-1", "status": "rejected", "result": "unknown_action"})
    svc.scan_once(sb)
    assert sb.one("ota_jobs", id="job-1")["error"] == "old_firmware"
