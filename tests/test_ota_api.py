"""OTA REST 통합 테스트 — /cameras/{id}/ota, /devices/{id}/ota, /firmware/*.

conftest 의 MagicMock 대신 FakeSB(인메모리)를 `fake_sb` 로 넘긴다 — ota_service 가 필터·정렬·갱신을
실제로 쓰기 때문. app_client 는 그대로(라우터·auth_device 에 같은 객체가 주입된다).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.crypto import generate_token, hash_token
from tests.conftest import OTHER_USER_ID, TEST_USER_ID
from tests.fake_sb import FakeSB

CAM_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"
DEV_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
REL_CAM = "rel-cam-1"
REL_DEV = "rel-dev-1"
CAM_TOKEN = generate_token()
CAM_TOKEN_HASH = hash_token(CAM_TOKEN)   # bcrypt 는 느려서 모듈당 1회


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _camera(**over: Any) -> dict:
    base = {
        "id": CAM_UUID, "owner_id": TEST_USER_ID, "camera_id": "p4cam-aabbccdd", "unlinked_at": None,
        "token_hash": CAM_TOKEN_HASH, "firmware_ver": "fb2-p4 0.2.1-20261006",
        "capabilities": {"ota": True}, "clip_stats": {"up_busy_s": -1, "sys": {"uptime_s": 3600, "rssi": -50, "int_largest": 70_000}},
        "last_seen_at": _iso(5), "is_online": True, "live_session_id": None,
    }
    base.update(over)
    return base


def _device(**over: Any) -> dict:
    base = {
        "id": DEV_UUID, "owner_id": TEST_USER_ID, "device_id": "terra-test01", "unlinked_at": None,
        "token_hash": CAM_TOKEN_HASH, "firmware_ver": "terra-fw 1.0.0", "capabilities": {"ota": True},
        "sys_state": {"uptime_s": 3600, "rssi": -50, "int_largest": 90_000},
        "last_seen_at": _iso(5), "is_online": True,
    }
    base.update(over)
    return base


def _releases() -> list[dict]:
    return [
        {"id": REL_CAM, "target": "camera_p4", "version": "fb2-p4 0.3.0-20261010",
         "r2_key": "firmware/camera_p4/v3.bin", "size_bytes": 10, "sha256": "a" * 64, "created_at": _iso(100)},
        {"id": REL_DEV, "target": "device_nano", "version": "terra-fw 1.1.0",
         "r2_key": "firmware/device_nano/v11.bin", "size_bytes": 10, "sha256": "b" * 64, "created_at": _iso(50)},
    ]


@pytest.fixture
def fake_sb() -> FakeSB:   # conftest 의 MagicMock 픽스처를 모듈 범위에서 덮어씀
    return FakeSB(cameras=[_camera()], devices=[_device()], firmware_releases=_releases())


class _PublishStub:
    calls: list[tuple[str, dict]] = []
    fail = False

    def publish(self, camera_id: str, command: dict, **_k: Any) -> None:
        if _PublishStub.fail:
            raise RuntimeError("broker down")
        _PublishStub.calls.append((camera_id, command))


@pytest.fixture(autouse=True)
def _stub_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.routers import cameras as cameras_router
    _PublishStub.calls = []
    _PublishStub.fail = False
    monkeypatch.setattr(cameras_router, "MqttWebRTCSignaling", _PublishStub)
    monkeypatch.setenv("API_PUBLIC_BASE_URL", "https://api.test")


# ---------- 카메라 ----------

def test_camera_ota_creates_job_and_publishes_prepare(app_client: TestClient, fake_sb: FakeSB) -> None:
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": REL_CAM})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "pending" and body["published"] is True
    assert body["version"] == "fb2-p4 0.3.0-20261010"

    cam_id, cmd = _PublishStub.calls[0]
    assert cam_id == "p4cam-aabbccdd"
    assert cmd["action"] == "ota_prepare" and cmd["ttl_sec"] == 60
    assert cmd["job_id"] == body["job_id"]
    assert cmd["url"] == f"https://api.test/firmware/jobs/{body['job_id']}/bin"
    assert cmd["size"] == 10 and cmd["sha256"] == "a" * 64 and cmd["version"] == body["version"]
    assert cmd["msg_id"] == body["msg_id"]

    job = fake_sb.one("ota_jobs", id=body["job_id"])
    assert job["prepare_msg_id"] == body["msg_id"]
    assert job["prev_version"] == "fb2-p4 0.2.1-20261006"


def test_camera_ota_gate_failure_409(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.one("cameras", id=CAM_UUID)["is_online"] = False
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": REL_CAM})
    assert res.status_code == 409
    assert "offline" in res.json()["detail"]
    assert _PublishStub.calls == [] and fake_sb.rows("ota_jobs") == []


def test_camera_ota_force_bypasses_gate_and_reports_reasons(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.one("cameras", id=CAM_UUID)["capabilities"] = None
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": REL_CAM, "force": True})
    assert res.status_code == 201, res.text
    assert res.json()["gate_reasons"] == ["no_ota_capability"]
    assert fake_sb.rows("ota_jobs")[0]["forced"] is True


def test_camera_ota_wrong_target_release_400(app_client: TestClient) -> None:
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": REL_DEV})
    assert res.status_code == 400


def test_camera_ota_other_owner_404(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.one("cameras", id=CAM_UUID)["owner_id"] = OTHER_USER_ID
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": REL_CAM})
    assert res.status_code == 404


def test_camera_ota_publish_failure_marks_failed(app_client: TestClient, fake_sb: FakeSB) -> None:
    _PublishStub.fail = True
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": REL_CAM})
    assert res.status_code == 201
    assert res.json()["published"] is False and res.json()["status"] == "failed"
    job = fake_sb.rows("ota_jobs")[0]
    assert (job["status"], job["error"]) == ("failed", "publish_failed")


def test_camera_apply_requires_ready(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("ota_jobs").append({"id": "job-1", "kind": "camera", "target_uuid": CAM_UUID,
                                     "release_id": REL_CAM, "status": "downloading", "created_at": _iso(1)})
    res = app_client.post(f"/cameras/{CAM_UUID}/ota/job-1/apply")
    assert res.status_code == 409
    assert _PublishStub.calls == []


def test_camera_apply_publishes_and_marks_applying(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("ota_jobs").append({"id": "job-1", "kind": "camera", "target_uuid": CAM_UUID,
                                     "release_id": REL_CAM, "status": "ready", "created_at": _iso(1)})
    res = app_client.post(f"/cameras/{CAM_UUID}/ota/job-1/apply")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "applying" and res.json()["published"] is True
    _, cmd = _PublishStub.calls[0]
    assert cmd["action"] == "ota_apply" and cmd["job_id"] == "job-1"
    job = fake_sb.one("ota_jobs", id="job-1")
    assert job["status"] == "applying" and job["apply_msg_id"] == cmd["msg_id"] and job["applied_at"]


def test_camera_apply_job_of_other_camera_404(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("ota_jobs").append({"id": "job-x", "kind": "camera", "target_uuid": "someone-else",
                                     "release_id": REL_CAM, "status": "ready", "created_at": _iso(1)})
    assert app_client.post(f"/cameras/{CAM_UUID}/ota/job-x/apply").status_code == 404


# ---------- 기기 (commands 경유) ----------

def test_device_ota_queues_prepare_command(app_client: TestClient, fake_sb: FakeSB) -> None:
    res = app_client.post(f"/devices/{DEV_UUID}/ota", json={"release_id": REL_DEV})
    assert res.status_code == 201, res.text
    body = res.json()
    cmd = fake_sb.rows("commands")[0]
    assert cmd["action"] == "ota_prepare" and cmd["ttl_sec"] == 60 and cmd["status"] == "pending"
    assert cmd["payload"]["job_id"] == body["job_id"]
    assert cmd["payload"]["url"].endswith(f"/firmware/jobs/{body['job_id']}/bin")
    assert cmd["device_id"] == DEV_UUID and cmd["reason"] == "ota_prepare"
    assert body["command_id"] == cmd["id"]
    assert fake_sb.one("ota_jobs", id=body["job_id"])["prepare_msg_id"] == cmd["id"]


def test_device_ota_apply_queues_apply_command(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("ota_jobs").append({"id": "job-d", "kind": "device", "target_uuid": DEV_UUID,
                                     "release_id": REL_DEV, "status": "ready", "created_at": _iso(1)})
    res = app_client.post(f"/devices/{DEV_UUID}/ota/job-d/apply")
    assert res.status_code == 200, res.text
    cmd = fake_sb.rows("commands")[0]
    assert cmd["action"] == "ota_apply" and cmd["payload"] == {"job_id": "job-d"}
    assert fake_sb.one("ota_jobs", id="job-d")["apply_msg_id"] == cmd["id"]


def test_device_ota_rejects_camera_release(app_client: TestClient) -> None:
    assert app_client.post(f"/devices/{DEV_UUID}/ota", json={"release_id": REL_CAM}).status_code == 400


# ---------- /firmware ----------

def test_list_releases_filters_target(app_client: TestClient) -> None:
    res = app_client.get("/firmware/releases", params={"target": "device_nano"})
    assert res.status_code == 200
    assert [r["version"] for r in res.json()] == ["terra-fw 1.1.0"]
    assert "r2_key" not in res.json()[0]


def test_list_releases_hides_retired_unless_asked(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("firmware_releases").append(
        {"id": "rel-cam-bad", "target": "camera_p4", "version": "fb2-p4 0.3.2-20261007",
         "r2_key": "firmware/camera_p4/bad.bin", "size_bytes": 10, "sha256": "c" * 64, "created_at": _iso(10),
         "retired_at": _iso(1), "retired_reason": "prepare 스택 버그"})
    shown = app_client.get("/firmware/releases", params={"target": "camera_p4"}).json()
    assert [r["id"] for r in shown] == [REL_CAM]
    all_ = app_client.get("/firmware/releases", params={"target": "camera_p4", "include_retired": "true"}).json()
    assert [r["id"] for r in all_] == ["rel-cam-bad", REL_CAM]
    assert all_[0]["retired_reason"] == "prepare 스택 버그"
    # 퇴역 릴리스로는 작업을 못 만든다 (force 도 무시)
    res = app_client.post(f"/cameras/{CAM_UUID}/ota", json={"release_id": "rel-cam-bad", "force": True})
    assert res.status_code == 409 and "retired" in res.json()["detail"]


def test_download_bin_retired_release_410(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("firmware_releases")[0]["retired_at"] = _iso(1)
    fake_sb.rows("ota_jobs").append(
        {"id": "job-r", "kind": "camera", "target_uuid": CAM_UUID, "release_id": REL_CAM, "status": "accepted",
         "created_at": _iso(1), "updated_at": _iso(1)})
    res = app_client.get("/firmware/jobs/job-r/bin", headers={"Authorization": f"Bearer {CAM_TOKEN}"})
    assert res.status_code == 410


def test_list_jobs_only_own_entities(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("ota_jobs").extend([
        {"id": "mine", "kind": "camera", "target_uuid": CAM_UUID, "release_id": REL_CAM, "status": "ready",
         "created_at": _iso(1), "updated_at": _iso(1)},
        {"id": "theirs", "kind": "camera", "target_uuid": "other-cam", "release_id": REL_CAM, "status": "ready",
         "created_at": _iso(1), "updated_at": _iso(1)},
    ])
    res = app_client.get("/firmware/jobs")
    assert [j["id"] for j in res.json()] == ["mine"]
    assert res.json()[0]["version"] == "fb2-p4 0.3.0-20261010"
    assert app_client.get("/firmware/jobs/theirs").status_code == 404
    assert app_client.get("/firmware/jobs", params={"target_uuid": "other-cam"}).status_code == 404


class _Body:
    def __init__(self, data: bytes) -> None:
        self._d = data
        self.closed = False

    def iter_chunks(self, n: int):
        for i in range(0, len(self._d), n):
            yield self._d[i:i + n]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def r2_stub(monkeypatch: pytest.MonkeyPatch) -> dict:
    from backend.routers import firmware as fw_router
    seen: dict[str, Any] = {}

    class _Client:
        def get_object(self, Bucket: str, Key: str) -> dict:
            seen["bucket"], seen["key"] = Bucket, Key
            return {"Body": _Body(b"0123456789")}

    monkeypatch.setattr(fw_router, "get_r2_client", lambda: _Client())
    monkeypatch.setattr(fw_router, "get_r2_bucket", lambda: "fw-bucket")
    return seen


def _job(status: str = "accepted", **over: Any) -> dict:
    base = {"id": "job-1", "kind": "camera", "target_uuid": CAM_UUID, "release_id": REL_CAM,
            "status": status, "pct": 0, "prepare_msg_id": "msg-p", "created_at": _iso(1), "updated_at": _iso(1)}
    base.update(over)
    return base


def test_download_bin_streams_with_camera_token(app_client: TestClient, fake_sb: FakeSB, r2_stub: dict) -> None:
    fake_sb.rows("ota_jobs").append(_job())
    res = app_client.get("/firmware/jobs/job-1/bin", headers={"Authorization": f"Bearer {CAM_TOKEN}"})
    assert res.status_code == 200, res.text
    assert res.content == b"0123456789"
    assert res.headers["content-type"].startswith("application/octet-stream")
    assert res.headers["content-length"] == "10"
    assert res.headers["x-firmware-version"] == "fb2-p4 0.3.0-20261010"
    assert res.headers["x-firmware-sha256"] == "a" * 64
    assert r2_stub == {"bucket": "fw-bucket", "key": "firmware/camera_p4/v3.bin"}
    assert fake_sb.one("ota_jobs", id="job-1")["status"] == "downloading"


def test_download_bin_wrong_token_401(app_client: TestClient, fake_sb: FakeSB, r2_stub: dict) -> None:
    fake_sb.rows("ota_jobs").append(_job())
    res = app_client.get("/firmware/jobs/job-1/bin", headers={"Authorization": "Bearer nope"})
    assert res.status_code == 401
    assert "key" not in r2_stub


def test_download_bin_no_header_401(app_client: TestClient, fake_sb: FakeSB) -> None:
    fake_sb.rows("ota_jobs").append(_job())
    assert app_client.get("/firmware/jobs/job-1/bin").status_code == 401


def test_download_bin_token_of_other_entity_401(app_client: TestClient, fake_sb: FakeSB) -> None:
    """같은 토큰이라도 작업 대상이 다른 기기면 거절 — 작업 id 만 알아선 못 받는다."""
    fake_sb.rows("ota_jobs").append(_job(target_uuid="other-cam"))
    res = app_client.get("/firmware/jobs/job-1/bin", headers={"Authorization": f"Bearer {CAM_TOKEN}"})
    assert res.status_code == 401


@pytest.mark.parametrize("status", ["ready", "verified", "failed", "applying"])
def test_download_bin_not_downloadable_409(app_client: TestClient, fake_sb: FakeSB, status: str) -> None:
    fake_sb.rows("ota_jobs").append(_job(status))
    res = app_client.get("/firmware/jobs/job-1/bin", headers={"Authorization": f"Bearer {CAM_TOKEN}"})
    assert res.status_code == 409


def test_download_bin_unknown_job_404(app_client: TestClient) -> None:
    res = app_client.get("/firmware/jobs/nope/bin", headers={"Authorization": f"Bearer {CAM_TOKEN}"})
    assert res.status_code == 404
