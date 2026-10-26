"""clips 라우터 통합 테스트.

- 워커용 (Camera Token) 2개: upload-url, meta INSERT
- 사용자용 (JWT/dev) 3개: list, get url, delete
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.crypto import hash_token
from tests.conftest import TEST_USER_ID


CAMERA_ID_TEXT = "p4cam-aabbccdd"
CAMERA_UUID = "22222222-2222-4222-8222-222222222222"
ENC_ID = "11111111-2222-4333-8444-555555555555"
CLIP_ID = "abcdef12-3456-4890-abcd-ef1234567890"
CAMERA_TOKEN = "test-camera-token"


@pytest.fixture
def authed_camera_row() -> dict:
    return {
        "id": CAMERA_UUID,
        "owner_id": TEST_USER_ID,
        "enclosure_id": ENC_ID,
        "camera_id": CAMERA_ID_TEXT,
        "token_hash": hash_token(CAMERA_TOKEN),
        "name": "거실 카메라",
        "model": "esp32-p4",
    }


def _setup_camera_lookup(fake_sb: MagicMock, row: dict) -> None:
    """auth_camera 가 cameras 테이블 select 시 row 반환."""
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value.data = [row]


# ---------- 워커: upload-url ----------


def test_upload_url_returns_presigned(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips/upload-url",
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
        json={"started_at": "2026-05-27T12:00:00Z", "duration_sec": 10.0},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["url"].startswith("https://r2.test/put/")
    # key 포맷: terra-clips/clips/{camera_id_text}/{YYYY-MM-DD}/{HHMMSS}_{clip_id}.mp4
    # 날짜/시각은 KST 기준 — UTC 2026-05-27T12:00:00Z → KST 21:00:00 (같은 날)
    assert body["key"].startswith(f"terra-clips/clips/{CAMERA_ID_TEXT}/2026-05-27/210000_")
    assert body["key"].endswith(".mp4")
    assert body["clip_id"] in body["key"]
    assert body["expires_in"] == 300
    # with_thumbnail 기본 True → 썸네일 PUT URL 도 같이 발급
    assert body["thumbnail_url"].startswith("https://r2.test/put/")
    assert body["thumbnail_key"].endswith(".jpg")
    assert body["thumbnail_key"].replace(".jpg", ".mp4") == body["key"]


def test_upload_url_without_thumbnail(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips/upload-url",
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
        json={
            "started_at": "2026-05-27T12:00:00Z",
            "duration_sec": 10.0,
            "with_thumbnail": False,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["thumbnail_url"] is None
    assert body["thumbnail_key"] is None


def test_upload_url_requires_camera_token(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips/upload-url",
        json={"started_at": "2026-05-27T12:00:00Z", "duration_sec": 10.0},
    )
    assert res.status_code == 401


def test_upload_url_wrong_token(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips/upload-url",
        headers={"Authorization": "Bearer wrong-token"},
        json={"started_at": "2026-05-27T12:00:00Z", "duration_sec": 10.0},
    )
    assert res.status_code == 401


# ---------- 워커: clip meta INSERT ----------


def test_create_clip_meta_ok(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)
    clip_id = "abcdef12-3456-7890-abcd-ef1234567890"
    key = f"terra-clips/clips/{CAMERA_ID_TEXT}/2026-05-27/120000_{clip_id}.mp4"

    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": clip_id}
    ]

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips",
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
        json={
            "key": key,
            "started_at": "2026-05-27T12:00:00Z",
            "duration_sec": 10.0,
            "file_size": 1048576,
            "width": 1280,
            "height": 720,
            "fps": 24.0,
            "motion_score": 0.42,
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["id"] == clip_id

    insert_payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert insert_payload["id"] == clip_id
    assert insert_payload["camera_id"] == CAMERA_UUID
    assert insert_payload["enclosure_id"] == ENC_ID
    assert insert_payload["owner_id"] == TEST_USER_ID
    assert insert_payload["r2_key"] == key
    # 촬영 목적: terra-clips/clips/ key 에서 서버가 도출 → "production" (운영 승격)
    assert insert_payload["clip_purpose"] == "production"


def test_create_clip_meta_schedules_faststart(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """응답 뒤 BackgroundTasks 로 같은 key 의 faststart 변환이 예약된다(TestClient 는 응답 후 바로 실행)."""
    from backend.routers import clips as clips_router

    _setup_camera_lookup(fake_sb, authed_camera_row)
    called: list[str] = []
    monkeypatch.setattr(clips_router, "ensure_faststart_safe", called.append)
    clip_id = "abcdef12-3456-7890-abcd-ef1234567890"
    key = f"terra-clips/clips/{CAMERA_ID_TEXT}/2026-05-27/120000_{clip_id}.mp4"
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [{"id": clip_id}]
    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips",
        json={"key": key, "started_at": "2026-05-27T12:00:00Z", "duration_sec": 10.0},
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
    )
    assert res.status_code == 201, res.text
    assert called == [key]


def test_create_clip_meta_duplicate_is_idempotent(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    """펌웨어 0.4.13 의 meta POST 재시도: 같은 id 재INSERT(23505) 는 기존 행 id 로 201."""
    from postgrest.exceptions import APIError

    clip_id = "abcdef12-3456-7890-abcd-ef1234567890"
    key = f"terra-clips/clips/{CAMERA_ID_TEXT}/2026-05-27/120000_{clip_id}.mp4"

    # cameras / motion_clips 가 같은 select→eq→limit→execute 체인을 쓰므로 테이블 이름으로 가른다.
    cameras_tbl = MagicMock()
    cameras_tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        authed_camera_row
    ]
    clips_tbl = MagicMock()
    clips_tbl.insert.return_value.execute.side_effect = APIError(
        {"message": "duplicate key value violates unique constraint", "code": "23505"}
    )
    clips_tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"id": clip_id, "camera_id": CAMERA_UUID}
    ]
    fake_sb.table.side_effect = lambda name: cameras_tbl if name == "cameras" else clips_tbl

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips",
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
        json={
            "key": key,
            "started_at": "2026-05-27T12:00:00Z",
            "duration_sec": 10.0,
            "file_size": 1048576,
            "width": 1280,
            "height": 720,
            "fps": 24.0,
            "motion_score": 0.42,
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["id"] == clip_id


def test_create_clip_meta_rejects_key_for_other_camera(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)
    bad_key = "terra-clips/clips/picam-deadbeef/2026-05-27/120000_abcdef12-3456-7890-abcd-ef1234567890.mp4"

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips",
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
        json={
            "key": bad_key,
            "started_at": "2026-05-27T12:00:00Z",
            "duration_sec": 10.0,
        },
    )
    assert res.status_code == 400
    assert "camera_id" in res.json()["detail"]


def test_create_clip_meta_rejects_malformed_key(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)

    res = app_client.post(
        f"/cameras/{CAMERA_UUID}/clips",
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}"},
        json={
            "key": "wrong/format/key.mp4",
            "started_at": "2026-05-27T12:00:00Z",
            "duration_sec": 10.0,
        },
    )
    assert res.status_code == 400


# ---------- 사용자: list ----------


def _owned_lookup_chain(table: MagicMock) -> MagicMock:
    """select().eq(id).eq(owner_id).limit(1) 체인 끝."""
    return table.select.return_value.eq.return_value.eq.return_value.limit.return_value


def _setup_enclosure_lookup(fake_sb: MagicMock, owner_id: str = TEST_USER_ID) -> None:
    chain = _owned_lookup_chain(fake_sb.table.return_value)
    chain.execute.return_value.data = [{"owner_id": owner_id}]


def test_list_enclosure_clips_ok(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    # 첫 select 호출은 enclosure 확인, 두 번째는 motion_clips
    enclosure_select_chain = MagicMock()
    enclosure_select_chain.execute.return_value.data = [{"owner_id": TEST_USER_ID}]

    clips_chain = MagicMock()
    clip_row = {
        "id": CLIP_ID,
        "camera_id": CAMERA_UUID,
        "enclosure_id": ENC_ID,
        "started_at": "2026-05-27T12:00:00Z",
        "duration_sec": 10.0,
        "r2_key": f"test/{CAMERA_ID_TEXT}/2026-05-27/120000_{CLIP_ID}.mp4",
        "thumbnail_key": None,
        "file_size": 1024,
        "width": 1280,
        "height": 720,
        "fps": 24.0,
        "codec": "h264",
        "container": "mp4",
        "motion_score": 0.5,
        "created_at": "2026-05-27T12:00:01Z",
    }
    clips_chain.execute.return_value.data = [clip_row]

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "enclosures":
            t.select.return_value.eq.return_value.eq.return_value.limit.return_value = (
                enclosure_select_chain
            )
        else:  # motion_clips
            t.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value = (
                clips_chain
            )
        return t

    fake_sb.table.side_effect = _table

    res = app_client.get(f"/enclosures/{ENC_ID}/clips")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == CLIP_ID
    assert body["has_more"] is False


def test_list_enclosure_clips_not_owner(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    _setup_enclosure_lookup(fake_sb, owner_id="other-user")

    res = app_client.get(f"/enclosures/{ENC_ID}/clips")
    assert res.status_code == 404


# ---------- 사용자: clip url ----------


def _setup_clip_lookup(fake_sb: MagicMock, owner_id: str = TEST_USER_ID) -> dict:
    row = {
        "id": CLIP_ID,
        "camera_id": CAMERA_UUID,
        "owner_id": owner_id,
        "r2_key": f"test/{CAMERA_ID_TEXT}/2026-05-27/120000_{CLIP_ID}.mp4",
        "thumbnail_key": None,
    }
    chain = _owned_lookup_chain(fake_sb.table.return_value)
    chain.execute.return_value.data = [row]
    return row


def test_get_clip_url_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    row = _setup_clip_lookup(fake_sb)
    res = app_client.get(f"/clips/{CLIP_ID}/url")
    assert res.status_code == 200
    body = res.json()
    assert body["url"].startswith("https://r2.test/get/")
    assert row["r2_key"] in body["url"]
    assert body["expires_in"] == 3600


def test_get_clip_url_not_owner(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    _setup_clip_lookup(fake_sb, owner_id="other-user")
    res = app_client.get(f"/clips/{CLIP_ID}/url")
    assert res.status_code == 404


# ---------- 사용자: delete ----------


def test_delete_clip_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    select_chain = MagicMock()
    select_chain.execute.return_value.data = [{
        "id": CLIP_ID,
        "camera_id": CAMERA_UUID,
        "owner_id": TEST_USER_ID,
        "r2_key": f"test/{CAMERA_ID_TEXT}/2026-05-27/120000_{CLIP_ID}.mp4",
        "thumbnail_key": None,
    }]
    delete_chain = MagicMock()
    delete_chain.execute.return_value.data = [{"id": CLIP_ID}]

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        t.select.return_value.eq.return_value.eq.return_value.limit.return_value = select_chain
        t.delete.return_value.eq.return_value.eq.return_value = delete_chain
        return t

    fake_sb.table.side_effect = _table

    res = app_client.delete(f"/clips/{CLIP_ID}")
    assert res.status_code == 204


def test_delete_clip_not_found_in_db(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    select_chain = MagicMock()
    select_chain.execute.return_value.data = []  # 없음 (limit(1) 리스트)

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        t.select.return_value.eq.return_value.eq.return_value.limit.return_value = select_chain
        return t

    fake_sb.table.side_effect = _table

    res = app_client.delete("/clips/99999999-0000-4000-8000-000000000000")
    assert res.status_code == 404


# ---------- 사용자 입력 id 검증 (.single() 500 회귀) ----------


@pytest.mark.parametrize("path", ["/clips/not-a-uuid/url", "/clips/clip-1/url"])
def test_get_clip_url_bad_uuid_404(
    app_client: TestClient, fake_sb: MagicMock, path: str
) -> None:
    """uuid 아닌 id 는 DB 에 안 보내고 404 (실 PostgREST 는 22P02 → 500)."""
    res = app_client.get(path)
    assert res.status_code == 404
    fake_sb.table.assert_not_called()


@pytest.mark.parametrize("suffix", ["/url", "/thumbnail/url"])
def test_get_clip_urls_missing_404(
    app_client: TestClient, fake_sb: MagicMock, suffix: str
) -> None:
    """0행은 빈 리스트 → 404 (.single() 이었다면 실 DB 에서 406 → 500)."""
    _owned_lookup_chain(fake_sb.table.return_value).execute.return_value.data = []
    res = app_client.get(f"/clips/{CLIP_ID}{suffix}")
    assert res.status_code == 404
    assert res.json()["detail"] == "clip not found"


def test_get_thumbnail_url_bad_uuid_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    res = app_client.get("/clips/not-a-uuid/thumbnail/url")
    assert res.status_code == 404
    fake_sb.table.assert_not_called()


def test_clip_lookup_filters_by_owner(app_client: TestClient, fake_sb: MagicMock) -> None:
    """service_role 은 RLS 바이패스 → 쿼리에 owner_id 필터가 걸려야 한다."""
    _setup_clip_lookup(fake_sb)
    app_client.get(f"/clips/{CLIP_ID}/url")
    select = fake_sb.table.return_value.select.return_value
    select.eq.assert_called_with("id", CLIP_ID)
    select.eq.return_value.eq.assert_called_with("owner_id", TEST_USER_ID)
    select.eq.return_value.eq.return_value.limit.assert_called_with(1)


# ---------- 사용자: labels ----------


def test_upsert_label_bad_uuid_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    res = app_client.post("/clips/not-a-uuid/labels", json={"action": "drinking"})
    assert res.status_code == 404
    fake_sb.table.assert_not_called()


def test_upsert_label_missing_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    _owned_lookup_chain(fake_sb.table.return_value).execute.return_value.data = []
    res = app_client.post(f"/clips/{CLIP_ID}/labels", json={"action": "drinking"})
    assert res.status_code == 404
    fake_sb.table.return_value.upsert.assert_not_called()


def test_upsert_label_uses_normalized_clip_id(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """urn:uuid: 형식도 uuid.UUID 는 받지만 Postgres 캐스트는 거부 → row 의 정규화 id 로 저장."""
    _setup_clip_lookup(fake_sb)
    res = app_client.post(
        f"/clips/urn:uuid:{CLIP_ID.upper()}/labels", json={"action": "drinking"}
    )
    assert res.status_code == 201, res.text
    upserted = fake_sb.table.return_value.upsert.call_args.args[0]
    assert upserted["clip_id"] == CLIP_ID
    assert res.json()["clip_id"] == CLIP_ID
    # 조회도 정규화된 id 로
    fake_sb.table.return_value.select.return_value.eq.assert_called_with("id", CLIP_ID)


def test_delete_clip_bad_uuid_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    res = app_client.delete("/clips/missing")
    assert res.status_code == 404
    fake_sb.table.assert_not_called()


def test_list_enclosure_clips_bad_uuid_404(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    res = app_client.get("/enclosures/enc-uuid/clips")
    assert res.status_code == 404
    fake_sb.table.assert_not_called()


def test_list_enclosure_clips_missing_404(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    _owned_lookup_chain(fake_sb.table.return_value).execute.return_value.data = []
    res = app_client.get(f"/enclosures/{ENC_ID}/clips")
    assert res.status_code == 404


def test_delete_clip_urn_uuid_uses_normalized_id(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """urn:uuid: 원본을 DELETE 에 쓰면 R2 삭제 뒤 22P02 500 → row 의 정규화 id 로 지운다."""
    select_chain = MagicMock()
    select_chain.execute.return_value.data = [{
        "id": CLIP_ID,
        "camera_id": CAMERA_UUID,
        "owner_id": TEST_USER_ID,
        "r2_key": f"test/{CAMERA_ID_TEXT}/2026-05-27/120000_{CLIP_ID}.mp4",
        "thumbnail_key": None,
    }]
    t = MagicMock()
    t.select.return_value.eq.return_value.eq.return_value.limit.return_value = select_chain
    t.delete.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": CLIP_ID}]
    fake_sb.table.side_effect = lambda name: t

    res = app_client.delete(f"/clips/urn:uuid:{CLIP_ID.upper()}")
    assert res.status_code == 204, res.text
    t.select.return_value.eq.assert_called_with("id", CLIP_ID)
    t.delete.return_value.eq.assert_called_with("id", CLIP_ID)
    t.delete.return_value.eq.return_value.eq.assert_called_with("owner_id", TEST_USER_ID)
