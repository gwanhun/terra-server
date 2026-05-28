"""
공통 테스트 픽스처.

전략:
- AUTH_MODE=dev + DEV_USER_ID 환경변수 설정 → JWT 검증 우회
- backend.supabase_client.get_supabase_client 를 MagicMock 으로 override
- backend.r2_client 의 presigned URL/delete 함수도 monkeypatch
- 실제 Supabase / R2 에 연결 안 함
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# 테스트용 고정 user_id (UUID 형식)
TEST_USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _set_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 테스트에서 AUTH_MODE=dev + DEV_USER_ID 적용."""
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_USER_ID", TEST_USER_ID)
    # supabase_client 의 placeholder 검사를 통과시킬 더미 값
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
    # R2 더미 (실제 boto3 client 만들지 않음 — get_r2_client 는 monkeypatch)
    monkeypatch.setenv("R2_ENDPOINT", "https://test.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("R2_BUCKET", "test-bucket")


@pytest.fixture
def fake_sb() -> MagicMock:
    """Supabase fluent API 모킹. chain 호출은 MagicMock 자동 처리.

    사용 예:
        fake_sb.table("devices").insert.return_value.execute.return_value.data = [...]

    table() 호출별로 별도 mock 을 원하면 side_effect 사용.
    """
    return MagicMock()


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock) -> TestClient:
    """FastAPI TestClient + Supabase / R2 mock 주입."""
    # supabase_client.get_supabase_client 캐시 비우고 mock 반환
    from backend import supabase_client

    supabase_client.reset_client_cache()
    monkeypatch.setattr(supabase_client, "get_supabase_client", lambda: fake_sb)

    # auth_camera 와 routers 내부에서도 동일 mock 가도록 module-level patch
    from backend import auth_camera
    from backend.routers import cameras as cameras_router
    from backend.routers import clips as clips_router
    from backend.routers import devices as devices_router
    from backend.routers import enclosures as enclosures_router

    for mod in (
        auth_camera,
        cameras_router,
        clips_router,
        devices_router,
        enclosures_router,
    ):
        monkeypatch.setattr(mod, "get_supabase_client", lambda: fake_sb)

    # R2 함수 stub (실제 boto3 호출 없음)
    monkeypatch.setattr(
        clips_router,
        "generate_presigned_put_url",
        lambda key, expires_in=300: f"https://r2.test/put/{key}?sig=test",
    )
    monkeypatch.setattr(
        clips_router,
        "generate_presigned_get_url",
        lambda key, expires_in=3600: f"https://r2.test/get/{key}?sig=test",
    )
    monkeypatch.setattr(clips_router, "delete_object", lambda key: None)

    from backend.main import app

    return TestClient(app)
