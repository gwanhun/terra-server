"""auth_camera (Camera Token Bearer) 단위 테스트.

라우터 없이 dependency 함수 직접 호출 + Supabase mock.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.auth_camera import CameraAuthError, get_authed_camera
from backend.crypto import generate_token, hash_token


def _setup_camera_row(fake_sb: MagicMock, token: str, camera_id: str = "cam-uuid") -> dict:
    """fake_sb 가 cameras.select.eq.single.execute → 주어진 row 반환하도록 설정."""
    row = {
        "id": camera_id,
        "owner_id": "owner-1",
        "enclosure_id": "enc-1",
        "camera_id": "p4cam-aabbccdd",
        "token_hash": hash_token(token),
        "name": "거실 카메라",
        "model": "esp32-p4",
    }
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = row
    return row


def test_missing_authorization_header(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)

    with pytest.raises(CameraAuthError, match="Authorization 헤더가 없음"):
        get_authed_camera(camera_id="cam-uuid", authorization=None)


def test_bad_authorization_format(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)

    with pytest.raises(CameraAuthError, match="Bearer"):
        get_authed_camera(camera_id="cam-uuid", authorization="Token abcdef")


def test_camera_not_found(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = None

    with pytest.raises(CameraAuthError):
        get_authed_camera(camera_id="cam-uuid", authorization="Bearer anytoken")


def test_token_mismatch(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    _setup_camera_row(fake_sb, token="correct-token")

    with pytest.raises(CameraAuthError):
        get_authed_camera(camera_id="cam-uuid", authorization="Bearer wrong-token")


def test_token_valid_returns_row(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    from backend import auth_camera

    monkeypatch.setattr(auth_camera, "get_supabase_client", lambda: fake_sb)
    token = generate_token()
    expected = _setup_camera_row(fake_sb, token=token)

    row = get_authed_camera(camera_id="cam-uuid", authorization=f"Bearer {token}")
    assert row["id"] == expected["id"]
    assert row["camera_id"] == expected["camera_id"]
