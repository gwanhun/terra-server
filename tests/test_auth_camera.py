"""auth_camera (Camera Token Bearer) 단위 테스트.

라우터 없이 dependency 함수 직접 호출 + Supabase mock.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.auth_camera import CameraAuthError, get_authed_camera
from backend.crypto import generate_token, hash_token

CAMERA_UUID = "22222222-2222-4222-8222-222222222222"


def _setup_camera_row(fake_sb: MagicMock, token: str, camera_id: str = CAMERA_UUID) -> dict:
    """fake_sb 가 cameras.select.eq.limit.execute → [row] 반환하도록 설정."""
    row = {
        "id": camera_id,
        "owner_id": "owner-1",
        "enclosure_id": "55555555-5555-4555-8555-555555555555",
        "camera_id": "p4cam-aabbccdd",
        "token_hash": hash_token(token),
        "name": "거실 카메라",
        "model": "esp32-p4",
    }
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value.data = [row]
    return row


def test_missing_authorization_header(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)

    with pytest.raises(CameraAuthError, match="Authorization 헤더가 없음"):
        get_authed_camera(camera_id=CAMERA_UUID, authorization=None)


def test_bad_authorization_format(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)

    with pytest.raises(CameraAuthError, match="Bearer"):
        get_authed_camera(camera_id=CAMERA_UUID, authorization="Token abcdef")


def test_camera_not_found(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value.data = []  # 0행 (실 PostgREST .single() 이었다면 406 → 500)

    with pytest.raises(CameraAuthError):
        get_authed_camera(camera_id=CAMERA_UUID, authorization="Bearer anytoken")


def test_token_mismatch(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    _setup_camera_row(fake_sb, token="correct-token")

    with pytest.raises(CameraAuthError):
        get_authed_camera(camera_id=CAMERA_UUID, authorization="Bearer wrong-token")


def test_token_valid_returns_row(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    token = generate_token()
    expected = _setup_camera_row(fake_sb, token=token)

    row = get_authed_camera(camera_id=CAMERA_UUID, authorization=f"Bearer {token}")
    assert row["id"] == expected["id"]
    assert row["camera_id"] == expected["camera_id"]


@pytest.mark.parametrize("camera_id", ["cam-uuid", "p4cam-aabbccdd", ""])
def test_non_uuid_camera_id_is_401_without_db(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock, camera_id: str
) -> None:
    """uuid 아닌 path id 는 DB 로 안 보내고 미존재와 같은 401 (실 DB 는 22P02 → 500)."""
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    with pytest.raises(CameraAuthError):
        get_authed_camera(camera_id=camera_id, authorization="Bearer anytoken")
    fake_sb.table.assert_not_called()
