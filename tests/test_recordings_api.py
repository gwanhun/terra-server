"""라이브 직접 녹화 API (Stage L) — /recordings.

conftest 의 app_client(MagicMock) 대신 FakeSB + dict 기반 R2 stub 을 이 파일 전용 fixture 로 쓴다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from tests.conftest import OTHER_USER_ID, TEST_USER_ID
from tests.fake_sb import FakeSB

CAM = "33333333-3333-3333-3333-333333333333"
CAM2 = "44444444-4444-4444-4444-444444444444"
OTHER_CAM = "55555555-5555-5555-5555-555555555555"
MB = 1024 * 1024


@dataclass
class R2Stub:
    objects: dict[str, int] = field(default_factory=dict)       # key → size
    put_calls: list[tuple[str, str, int]] = field(default_factory=list)
    get_calls: list[tuple[str, int]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    fail_put: bool = False
    fail_get: bool = False
    fail_head: bool = False
    fail_delete: bool = False

    @staticmethod
    def _err(op: str) -> ClientError:
        return ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, op)

    def put_url(self, key: str, content_type: str = "video/mp4", expires_in: int = 300) -> str:
        if self.fail_put:
            raise self._err("PutObject")
        self.put_calls.append((key, content_type, expires_in))
        return f"https://r2.test/put/{key}?sig=test"

    def get_url(self, key: str, expires_in: int = 3600) -> str:
        if self.fail_get:
            raise self._err("GetObject")
        self.get_calls.append((key, expires_in))
        return f"https://r2.test/get/{key}?sig=test"

    def head(self, key: str) -> int | None:
        if self.fail_head:
            raise self._err("HeadObject")
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise self._err("DeleteObject")
        self.deleted.append(key)
        self.objects.pop(key, None)


class Env:
    def __init__(self, sb: FakeSB, r2: R2Stub, client: TestClient) -> None:
        self.sb = sb
        self.r2 = r2
        self.client = client

    @property
    def recs(self) -> list[dict[str, Any]]:
        return self.sb.rows("live_recordings")


def _cameras() -> list[dict[str, Any]]:
    return [
        {"id": CAM, "owner_id": TEST_USER_ID, "unlinked_at": None},
        {"id": CAM2, "owner_id": TEST_USER_ID, "unlinked_at": None},
        {"id": OTHER_CAM, "owner_id": OTHER_USER_ID, "unlinked_at": None},
    ]


def _make_env(monkeypatch: pytest.MonkeyPatch, sb: FakeSB) -> Env:
    from backend.routers import recordings

    r2 = R2Stub()
    monkeypatch.setattr(recordings, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(recordings, "generate_presigned_put_url", r2.put_url)
    monkeypatch.setattr(recordings, "generate_presigned_get_url", r2.get_url)
    monkeypatch.setattr(recordings, "head_object_size", r2.head)
    monkeypatch.setattr(recordings, "delete_object", r2.delete)

    from backend.main import app

    return Env(sb, r2, TestClient(app))


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    return _make_env(monkeypatch, FakeSB(cameras=_cameras(), live_recordings=[]))


def _body(**over: Any) -> dict[str, Any]:
    b: dict[str, Any] = {
        "camera_id": CAM,
        "client_id": str(uuid.uuid4()),
        "started_at": "2026-10-20T10:00:00+00:00",
        "duration_ms": 30_000,
        "size_bytes": 10 * MB,
        "width": 1280,
        "height": 720,
        "has_thumbnail": True,
    }
    b.update(over)
    return b


def _key(rid: str, ext: str, cam: str = CAM, user: str = TEST_USER_ID) -> str:
    return f"terra-clips/recordings/{user}/{cam}/{rid}.{ext}"


def _rec(*, rid: str | None = None, client_id: str | None = None, status: str = "pending",
         user: str = TEST_USER_ID, cam: str = CAM, thumb: bool = True, hours_ago: float = 1.0,
         size_bytes: int = 10 * MB) -> dict[str, Any]:
    rid = rid or str(uuid.uuid4())
    return {
        "id": rid, "user_id": user, "camera_id": cam, "client_id": client_id or str(uuid.uuid4()),
        "status": status, "started_at": "2026-10-20T10:00:00+00:00", "duration_ms": 30_000,
        "size_bytes": size_bytes, "width": 1280, "height": 720,
        "r2_key": _key(rid, "mp4", cam, user),
        "thumbnail_key": _key(rid, "jpg", cam, user) if thumb else None,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
    }


# ---------------------------------------------------------------- POST /recordings


def test_create_returns_201_with_put_urls(env: Env) -> None:
    before = datetime.now(timezone.utc)
    res = env.client.post("/recordings", json=_body())
    assert res.status_code == 201, res.text
    data = res.json()
    rid = data["recording_id"]
    assert data["status"] == "pending"
    assert _key(rid, "mp4") in data["video_upload_url"]
    assert _key(rid, "jpg") in data["thumb_upload_url"]
    exp = datetime.fromisoformat(data["expires_at"])
    assert abs((exp - (before + timedelta(seconds=1800))).total_seconds()) < 10
    assert len(env.recs) == 1
    row = env.recs[0]
    assert row["id"] == rid and row["status"] == "pending" and row["user_id"] == TEST_USER_ID
    assert row["r2_key"] == _key(rid, "mp4") and row["thumbnail_key"] == _key(rid, "jpg")


def test_create_without_thumbnail_thumb_url_null(env: Env) -> None:
    res = env.client.post("/recordings", json=_body(has_thumbnail=False))
    assert res.status_code == 201
    assert res.json()["thumb_upload_url"] is None
    assert env.recs[0]["thumbnail_key"] is None


def test_create_other_users_camera_403(env: Env) -> None:
    res = env.client.post("/recordings", json=_body(camera_id=OTHER_CAM))
    assert res.status_code == 403
    assert res.json()["detail"] == "camera not found or not owned"
    assert env.recs == []


def test_create_unknown_camera_403(env: Env) -> None:
    res = env.client.post("/recordings", json=_body(camera_id=str(uuid.uuid4())))
    assert res.status_code == 403
    assert res.json()["detail"] == "camera not found or not owned"


def test_create_allows_unlinked_own_camera(env: Env) -> None:
    env.sb.rows("cameras")[0]["unlinked_at"] = "2026-10-19T00:00:00+00:00"
    res = env.client.post("/recordings", json=_body())
    assert res.status_code == 201


@pytest.mark.parametrize("duration", [2999, 60001])
def test_create_duration_out_of_range_400(env: Env, duration: int) -> None:
    res = env.client.post("/recordings", json=_body(duration_ms=duration))
    assert res.status_code == 400
    assert res.json()["detail"] == "duration_ms must be between 3000 and 60000"
    assert env.recs == []


def test_create_size_too_large_400(env: Env) -> None:
    res = env.client.post("/recordings", json=_body(size_bytes=30 * MB + 1))
    assert res.status_code == 400
    assert res.json()["detail"] == f"size_bytes must be between 1 and {30 * MB}"


def test_create_size_zero_400(env: Env) -> None:
    res = env.client.post("/recordings", json=_body(size_bytes=0))
    assert res.status_code == 400
    assert res.json()["detail"] == f"size_bytes must be between 1 and {30 * MB}"


def test_create_quota_exceeded_409(env: Env) -> None:
    env.recs.extend(_rec(status="pending" if i % 3 == 0 else "ready") for i in range(50))
    env.recs.append(_rec(user=OTHER_USER_ID, cam=OTHER_CAM))     # 남의 행은 안 센다
    res = env.client.post("/recordings", json=_body())
    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "recording_quota_exceeded", "used": 50, "limit": 50}
    assert len(env.recs) == 51


def test_create_under_quota_counts_only_own_rows(env: Env) -> None:
    env.recs.extend(_rec() for _ in range(49))
    env.recs.extend(_rec(user=OTHER_USER_ID, cam=OTHER_CAM) for _ in range(5))
    res = env.client.post("/recordings", json=_body())
    assert res.status_code == 201


def test_retry_same_client_id_pending_returns_200_new_urls(env: Env) -> None:
    body = _body()
    first = env.client.post("/recordings", json=body)
    assert first.status_code == 201
    second = env.client.post("/recordings", json=body)
    assert second.status_code == 200
    rid = first.json()["recording_id"]
    data = second.json()
    assert data["recording_id"] == rid
    assert data["status"] == "pending"
    assert _key(rid, "mp4") in data["video_upload_url"]
    assert _key(rid, "jpg") in data["thumb_upload_url"]
    assert data["expires_at"] is not None
    assert len(env.recs) == 1
    assert len(env.r2.put_calls) == 4      # URL 을 새로 발급했다


def test_retry_same_client_id_at_quota_skips_quota(env: Env) -> None:
    env.recs.extend(_rec() for _ in range(50))
    existing = env.recs[7]
    res = env.client.post("/recordings", json=_body(client_id=existing["client_id"]))
    assert res.status_code == 200
    assert res.json()["recording_id"] == existing["id"]
    assert len(env.recs) == 50


def test_retry_same_client_id_ready_returns_null_urls(env: Env) -> None:
    row = _rec(status="ready")
    env.recs.append(row)
    res = env.client.post("/recordings", json=_body(client_id=row["client_id"]))
    assert res.status_code == 200
    assert res.json() == {
        "recording_id": row["id"], "status": "ready",
        "video_upload_url": None, "thumb_upload_url": None, "expires_at": None,
    }
    assert env.r2.put_calls == []


class _DupOnInsert(FakeSB):
    """live_recordings INSERT 직전에 같은 client_id 행이 생긴 것처럼 만들고 23505 를 던진다."""

    def __init__(self, sneak_row: dict[str, Any], **tables: Any) -> None:
        super().__init__(**tables)
        self.sneak_row = sneak_row

    def table(self, name: str) -> Any:
        q = super().table(name)
        if name != "live_recordings":
            return q
        store = self.store
        sneak = self.sneak_row

        def _insert(payload: Any) -> Any:
            store["live_recordings"].append(dict(sneak))
            raise APIError({"message": "duplicate key", "code": "23505",
                            "details": None, "hint": None})

        q.insert = _insert
        return q


def test_create_concurrent_duplicate_returns_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    client_id = str(uuid.uuid4())
    winner = _rec(client_id=client_id, hours_ago=0)
    sb = _DupOnInsert(winner, cameras=_cameras(), live_recordings=[])
    e = _make_env(monkeypatch, sb)
    res = e.client.post("/recordings", json=_body(client_id=client_id))
    assert res.status_code == 200, res.text
    assert res.json()["recording_id"] == winner["id"]
    assert res.json()["status"] == "pending"
    assert _key(winner["id"], "mp4") in res.json()["video_upload_url"]
    assert len(e.recs) == 1


class _OtherApiErrorOnInsert(FakeSB):
    def table(self, name: str) -> Any:
        q = super().table(name)
        if name == "live_recordings":
            def _insert(payload: Any) -> Any:
                raise APIError({"message": "check violation", "code": "23514",
                                "details": None, "hint": None})
            q.insert = _insert
        return q


def test_create_other_api_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = _OtherApiErrorOnInsert(cameras=_cameras(), live_recordings=[])
    e = _make_env(monkeypatch, sb)
    with pytest.raises(APIError):
        e.client.post("/recordings", json=_body())


def test_retry_same_client_id_ignores_new_body(env: Env) -> None:
    body = _body()
    first = env.client.post("/recordings", json=body).json()
    rid = first["recording_id"]
    retry = dict(body, has_thumbnail=False, camera_id=CAM2, duration_ms=5000)
    res = env.client.post("/recordings", json=retry)
    assert res.status_code == 200
    data = res.json()
    assert data["recording_id"] == rid
    assert _key(rid, "mp4", CAM) in data["video_upload_url"]
    assert data["thumb_upload_url"] is not None and _key(rid, "jpg", CAM) in data["thumb_upload_url"]
    assert env.recs[0]["duration_ms"] == 30_000
    assert env.recs[0]["camera_id"] == CAM


def test_retry_stale_pending_registers_new(env: Env) -> None:
    old = _rec(hours_ago=25)
    env.recs.append(old)
    env.r2.objects[old["r2_key"]] = 123
    res = env.client.post("/recordings", json=_body(client_id=old["client_id"]))
    assert res.status_code == 201
    new_id = res.json()["recording_id"]
    assert new_id != old["id"]
    assert [r["id"] for r in env.recs] == [new_id]
    assert sorted(env.r2.deleted) == sorted([old["r2_key"], old["thumbnail_key"]])


def test_insert_payload_has_no_generated_column(env: Env) -> None:
    res = env.client.post("/recordings", json=_body())
    assert res.status_code == 201
    row = env.recs[0]
    assert "has_thumbnail" not in row
    # FakeSB 가 붙이는 created_at/updated_at 을 빼면 화이트리스트 그대로
    assert set(row) - {"created_at", "updated_at"} == {
        "id", "user_id", "camera_id", "client_id", "status", "started_at", "duration_ms",
        "size_bytes", "width", "height", "r2_key", "thumbnail_key",
    }


def test_create_put_urls_content_type_and_ttl(env: Env) -> None:
    res = env.client.post("/recordings", json=_body())
    rid = res.json()["recording_id"]
    assert env.r2.put_calls == [
        (_key(rid, "mp4"), "video/mp4", 1800),
        (_key(rid, "jpg"), "image/jpeg", 1800),
    ]


def test_create_presign_failure_502(env: Env) -> None:
    env.r2.fail_put = True
    res = env.client.post("/recordings", json=_body())
    assert res.status_code == 502
    assert isinstance(res.json()["detail"], str)


def test_create_malformed_body_400(env: Env) -> None:
    res = env.client.post("/recordings", json=_body(camera_id="abc"))
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert isinstance(detail, str) and detail.startswith("invalid request: ")
    assert "camera_id" in detail


def test_create_missing_field_400(env: Env) -> None:
    body = _body()
    del body["duration_ms"]
    res = env.client.post("/recordings", json=body)
    assert res.status_code == 400
    assert isinstance(res.json()["detail"], str)


def test_other_routes_keep_default_422(env: Env) -> None:
    """/recordings 외 경로는 FastAPI 기본 422 유지."""
    res = env.client.post("/devices/pair", json={})
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], list)
