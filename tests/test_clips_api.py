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
CAMERA_UUID = "cam-uuid"
ENC_ID = "enc-uuid"
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
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = row


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
    # key 포맷: clips/YYYY/MM/DD/{camera_id_text}/{clip_id}.mp4
    assert body["key"].startswith(f"clips/2026/05/27/{CAMERA_ID_TEXT}/")
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
    key = f"clips/2026/05/27/{CAMERA_ID_TEXT}/{clip_id}.mp4"

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


def test_create_clip_meta_rejects_key_for_other_camera(
    app_client: TestClient, fake_sb: MagicMock, authed_camera_row: dict
) -> None:
    _setup_camera_lookup(fake_sb, authed_camera_row)
    bad_key = "clips/2026/05/27/picam-deadbeef/abcdef12-3456-7890-abcd-ef1234567890.mp4"

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


def _setup_enclosure_lookup(fake_sb: MagicMock, owner_id: str = TEST_USER_ID) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = {"owner_id": owner_id}


def test_list_enclosure_clips_ok(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    # 첫 select 호출은 enclosure 확인, 두 번째는 motion_clips
    enclosure_select_chain = MagicMock()
    enclosure_select_chain.execute.return_value.data = {"owner_id": TEST_USER_ID}

    clips_chain = MagicMock()
    clip_row = {
        "id": "clip-1",
        "camera_id": CAMERA_UUID,
        "enclosure_id": ENC_ID,
        "started_at": "2026-05-27T12:00:00Z",
        "duration_sec": 10.0,
        "r2_key": f"clips/2026/05/27/{CAMERA_ID_TEXT}/clip-1.mp4",
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
            t.select.return_value.eq.return_value.single.return_value = (
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
    assert body["items"][0]["id"] == "clip-1"
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
        "id": "clip-1",
        "camera_id": CAMERA_UUID,
        "owner_id": owner_id,
        "r2_key": f"clips/2026/05/27/{CAMERA_ID_TEXT}/clip-1.mp4",
        "thumbnail_key": None,
    }
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = row
    return row


def test_get_clip_url_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    row = _setup_clip_lookup(fake_sb)
    res = app_client.get("/clips/clip-1/url")
    assert res.status_code == 200
    body = res.json()
    assert body["url"].startswith("https://r2.test/get/")
    assert row["r2_key"] in body["url"]
    assert body["expires_in"] == 3600


def test_get_clip_url_not_owner(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    _setup_clip_lookup(fake_sb, owner_id="other-user")
    res = app_client.get("/clips/clip-1/url")
    assert res.status_code == 404


# ---------- 사용자: delete ----------


def test_delete_clip_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    select_chain = MagicMock()
    select_chain.execute.return_value.data = {
        "id": "clip-1",
        "camera_id": CAMERA_UUID,
        "owner_id": TEST_USER_ID,
        "r2_key": f"clips/2026/05/27/{CAMERA_ID_TEXT}/clip-1.mp4",
        "thumbnail_key": None,
    }
    delete_chain = MagicMock()
    delete_chain.execute.return_value.data = [{"id": "clip-1"}]

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        t.select.return_value.eq.return_value.single.return_value = select_chain
        t.delete.return_value.eq.return_value.eq.return_value = delete_chain
        return t

    fake_sb.table.side_effect = _table

    res = app_client.delete("/clips/clip-1")
    assert res.status_code == 204


def test_delete_clip_not_found_in_db(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    select_chain = MagicMock()
    select_chain.execute.return_value.data = None  # 없음

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        t.select.return_value.eq.return_value.single.return_value = select_chain
        return t

    fake_sb.table.side_effect = _table

    res = app_client.delete("/clips/missing")
    assert res.status_code == 404
