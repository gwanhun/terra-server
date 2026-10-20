"""운영 카메라 재부팅 API (petcam-lab 요청 6) — POST /ops/cameras/{id}/reboot.

conftest 의 app_client(MagicMock) 대신 FakeSB + publish 캡처 stub 을 이 파일 전용 fixture 로 쓴다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fake_sb import FakeSB

CAM = "66666666-6666-6666-6666-666666666666"
UNLINKED_CAM = "77777777-7777-7777-7777-777777777777"
SECRET = "s3cret"
BODY = {"reason": "night_rec_stall", "requested_by": "beta-camera-watch"}


class Env:
    def __init__(self, sb: FakeSB, client: TestClient, calls: list[tuple[str, dict]]) -> None:
        self.sb = sb
        self.client = client
        self.calls = calls
        self.fail_publish = False

    @property
    def logs(self) -> list[dict[str, Any]]:
        return self.sb.rows("camera_ops_reboots")

    def post(self, cam: str = CAM, *, secret: str | None = SECRET,
             body: dict[str, Any] | None = None) -> Any:
        headers = {} if secret is None else {"X-Ops-Secret": secret}
        return self.client.post(f"/ops/cameras/{cam}/reboot", json=BODY if body is None else body,
                                headers=headers)


def _camera(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": CAM, "camera_id": "p4cam-beta", "owner_id": str(uuid.uuid4()),
        "labeling_group": "beta", "unlinked_at": None, "is_online": True,
        "live_session_id": None, "stream_until": None,
    }
    row.update(over)
    return row


def _make_env(monkeypatch: pytest.MonkeyPatch, sb: FakeSB) -> Env:
    from backend.routers import ops

    calls: list[tuple[str, dict]] = []
    env_holder: dict[str, Env] = {}

    class _FakeSignaling:
        def publish(self, camera_id: str, command: dict, **_: object) -> None:
            if env_holder["env"].fail_publish:
                raise RuntimeError("broker down")
            calls.append((camera_id, command))

    monkeypatch.setenv("OPS_REBOOT_SECRET", SECRET)
    monkeypatch.setattr(ops, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(ops, "MqttWebRTCSignaling", _FakeSignaling)

    from backend.main import app

    env = Env(sb, TestClient(app), calls)
    env_holder["env"] = env
    return env


def _env(monkeypatch: pytest.MonkeyPatch, *, cameras: list[dict[str, Any]] | None = None,
         **tables: list[dict[str, Any]]) -> Env:
    base: dict[str, list[dict[str, Any]]] = {"camera_ops_reboots": [], "ota_jobs": []}
    sb = FakeSB(cameras=cameras if cameras is not None else [_camera()], **{**base, **tables})
    return _make_env(monkeypatch, sb)


def _log(*, published: bool | None, seconds_ago: float) -> dict[str, Any]:
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return {"id": str(uuid.uuid4()), "camera_id": CAM, "msg_id": str(uuid.uuid4()),
            "reason": "night_rec_stall", "requested_by": "beta-camera-watch",
            "requested_at": at.isoformat(), "published": published,
            "ack_result": None, "acked_at": None}


# ---------------------------------------------------------------- 정상


def test_ops_reboot_publishes_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    res = env.post()
    assert res.status_code == 200
    body = res.json()
    assert body["published"] is True
    uuid.UUID(body["msg_id"])

    assert len(env.calls) == 1
    camera_id, cmd = env.calls[0]
    assert camera_id == "p4cam-beta"
    assert cmd["action"] == "reboot"
    assert cmd["msg_id"] == body["msg_id"]

    assert len(env.logs) == 1
    row = env.logs[0]
    assert row["published"] is True
    assert row["msg_id"] == body["msg_id"]
    assert row["camera_id"] == CAM
    assert row["reason"] == "night_rec_stall"
    assert row["requested_by"] == "beta-camera-watch"
    assert row["requested_at"]


# ---------------------------------------------------------------- 401


def test_ops_reboot_401_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    res = env.post(secret=None)
    assert res.status_code == 401
    assert res.json() == {"detail": "invalid ops secret"}
    assert env.calls == [] and env.logs == []


def test_ops_reboot_401_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    res = env.post(secret="wrong")
    assert res.status_code == 401
    assert env.calls == [] and env.logs == []


def test_ops_reboot_401_when_server_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    monkeypatch.delenv("OPS_REBOOT_SECRET", raising=False)
    res = env.post(secret="")
    assert res.status_code == 401
    assert env.calls == [] and env.logs == []


# ---------------------------------------------------------------- 404


def test_ops_reboot_404_unknown_and_unlinked(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch, cameras=[
        _camera(),
        _camera(id=UNLINKED_CAM, unlinked_at="2026-10-19T00:00:00+00:00"),
    ])
    for cam in (str(uuid.uuid4()), UNLINKED_CAM, "not-a-uuid"):
        res = env.post(cam)
        assert res.status_code == 404, cam
        assert res.json() == {"detail": "camera not found"}
    assert env.calls == [] and env.logs == []


# ---------------------------------------------------------------- 409


def test_ops_reboot_409_not_beta(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch, cameras=[_camera(labeling_group=None)])
    res = env.post()
    assert res.status_code == 409
    assert res.json() == {"detail": "skipped", "reason": "not_beta"}
    assert env.calls == [] and env.logs == []


def test_ops_reboot_409_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch, cameras=[_camera(is_online=False)])
    res = env.post()
    assert res.status_code == 409
    assert res.json() == {"detail": "skipped", "reason": "offline"}
    assert env.calls == [] and env.logs == []


def test_ops_reboot_409_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch, cameras=[_camera(live_session_id="s-1")])
    res = env.post()
    assert res.status_code == 409
    assert res.json() == {"detail": "skipped", "reason": "live_active"}
    assert env.calls == [] and env.logs == []


def test_ops_reboot_409_ota_active(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch, ota_jobs=[{
        "id": str(uuid.uuid4()), "target_uuid": CAM, "status": "downloading",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }])
    res = env.post()
    assert res.status_code == 409
    assert res.json() == {"detail": "skipped", "reason": "ota_active"}
    assert env.calls == [] and env.logs == []


def test_ops_reboot_live_by_stream_until_only(monkeypatch: pytest.MonkeyPatch) -> None:
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    env = _env(monkeypatch, cameras=[_camera(stream_until=future)])
    res = env.post()
    assert res.status_code == 409
    assert res.json()["reason"] == "live_active"

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    env = _env(monkeypatch, cameras=[_camera(stream_until=past)])
    res = env.post()
    assert res.status_code == 200
    assert res.json()["published"] is True


# ---------------------------------------------------------------- 쿨다운


def test_ops_reboot_429_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    assert env.post().status_code == 200
    res = env.post()
    assert res.status_code == 429
    body = res.json()
    assert body["detail"] == "cooldown"
    assert 7190 <= body["retry_after"] <= 7200
    assert res.headers["Retry-After"] == str(body["retry_after"])
    assert len(env.calls) == 1
    assert len(env.logs) == 1


def test_ops_reboot_cooldown_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    env.sb.rows("camera_ops_reboots").append(_log(published=True, seconds_ago=7201))
    res = env.post()
    assert res.status_code == 200
    assert res.json()["published"] is True


def test_ops_reboot_pending_row_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    env.sb.rows("camera_ops_reboots").append(_log(published=None, seconds_ago=10))
    res = env.post()
    assert res.status_code == 429
    assert env.calls == []


def test_ops_reboot_failed_publish_does_not_start_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    env.fail_publish = True
    res = env.post()
    assert res.status_code == 200
    assert res.json() == {"published": False, "msg_id": None}
    assert len(env.logs) == 1
    assert env.logs[0]["published"] is False

    env.fail_publish = False
    res = env.post()
    assert res.status_code == 200
    assert res.json()["published"] is True
    assert len(env.calls) == 1
    assert [r["published"] for r in env.logs] == [False, True]


# ---------------------------------------------------------------- fail-closed / 검증


def test_ops_reboot_insert_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    real_table = env.sb.table

    def _table(name: str) -> Any:
        q = real_table(name)
        if name == "camera_ops_reboots":
            def _boom(_payload: Any) -> Any:
                raise RuntimeError("relation camera_ops_reboots does not exist")
            q.insert = _boom  # type: ignore[method-assign]
        return q

    monkeypatch.setattr(env.sb, "table", _table)
    res = env.post()
    assert res.status_code == 503
    assert res.json() == {"detail": "ops log unavailable"}
    assert env.calls == []


def test_ops_reboot_422_missing_body_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(monkeypatch)
    res = env.post(body={"requested_by": "beta-camera-watch"})
    assert res.status_code == 422
    assert env.calls == [] and env.logs == []


# ---------------------------------------------------------------- /code-review 후속 (2026-10-20)


def test_ops_reboot_cooldown_sees_success_behind_many_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """2시간 안 성공 1건 뒤에 실패가 여러 건 쌓여도 쿨다운은 성공 행을 본다(조회 개수 제한 없음)."""
    env = _env(monkeypatch)
    rows = env.sb.rows("camera_ops_reboots")
    rows.append(_log(published=True, seconds_ago=3600))
    for i in range(6):
        rows.append(_log(published=False, seconds_ago=60 + i))
    res = env.post()
    assert res.status_code == 429
    assert env.calls == []


def test_ops_reboot_server_secret_whitespace_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env 에 붙은 공백·개행은 무시(push_events 시크릿과 같은 규칙)."""
    env = _env(monkeypatch)
    monkeypatch.setenv("OPS_REBOOT_SECRET", f" {SECRET}\r\n")
    assert env.post().status_code == 200


def test_ops_reboot_live_recheck_under_live_session_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """라이브 판정은 live_session.claim 과 같은 카메라 락 안에서 다시 읽는다 —
    락을 기다리는 사이 시청이 시작되면 재부팅하지 않는다."""
    import threading
    import time

    from backend import live_session

    env = _env(monkeypatch)
    out: dict[str, Any] = {}
    lock = live_session._lock_for(CAM)
    with lock:
        worker = threading.Thread(target=lambda: out.setdefault("res", env.post()))
        worker.start()
        time.sleep(0.3)
        assert worker.is_alive()   # 같은 락을 기다린다
        env.sb.rows("cameras")[0]["live_session_id"] = "s-late"   # 그 사이 시청 시작(claim)
    worker.join(5)
    res = out["res"]
    assert res.status_code == 409
    assert res.json() == {"detail": "skipped", "reason": "live_active"}
    assert env.calls == []
    assert env.logs == []
