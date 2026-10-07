"""auth_device — 기기/카메라 토큰 검증 (OTA 바이너리 다운로드 인증)."""

from __future__ import annotations

import pytest

from backend import auth_device
from backend.auth_device import DeviceAuthError, extract_bearer, verify_entity_token
from backend.crypto import generate_token, hash_token
from tests.fake_sb import FakeSB

TOKEN = generate_token()
HASH = hash_token(TOKEN)


@pytest.fixture
def sb(monkeypatch: pytest.MonkeyPatch) -> FakeSB:
    fake = FakeSB(
        devices=[{"id": "dev-1", "owner_id": "o", "token_hash": HASH, "unlinked_at": None},
                 {"id": "dev-gone", "owner_id": "o", "token_hash": HASH, "unlinked_at": "2026-10-01T00:00:00Z"}],
        cameras=[{"id": "cam-1", "owner_id": "o", "token_hash": HASH, "unlinked_at": None}],
    )
    monkeypatch.setattr(auth_device, "get_supabase_client", lambda: fake)
    return fake


def test_extract_bearer() -> None:
    assert extract_bearer("Bearer abc") == "abc"
    assert extract_bearer("bearer abc") == "abc"
    with pytest.raises(DeviceAuthError, match="없음"):
        extract_bearer(None)
    with pytest.raises(DeviceAuthError, match="Bearer"):
        extract_bearer("Token abc")


def test_verify_device_ok(sb: FakeSB) -> None:
    assert verify_entity_token("device", "dev-1", TOKEN)["id"] == "dev-1"


def test_verify_camera_ok(sb: FakeSB) -> None:
    assert verify_entity_token("camera", "cam-1", TOKEN)["id"] == "cam-1"


def test_verify_wrong_token_401(sb: FakeSB) -> None:
    with pytest.raises(DeviceAuthError):
        verify_entity_token("device", "dev-1", "wrong")


def test_verify_unlinked_401(sb: FakeSB) -> None:
    with pytest.raises(DeviceAuthError):
        verify_entity_token("device", "dev-gone", TOKEN)


def test_verify_unknown_401(sb: FakeSB) -> None:
    with pytest.raises(DeviceAuthError):
        verify_entity_token("camera", "nope", TOKEN)
