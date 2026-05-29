"""r2_client 단위 테스트."""

from __future__ import annotations

import pytest

from backend import r2_client
from backend.r2_client import (
    R2NotConfigured,
    generate_presigned_get_url,
    generate_presigned_put_url,
    get_r2_bucket,
    get_r2_client,
    reset_client_cache,
)


@pytest.fixture(autouse=True)
def _reset_r2_cache() -> None:
    """각 테스트 후 싱글톤 캐시 비움."""
    yield
    reset_client_cache()


def test_get_r2_bucket_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_BUCKET", "real-bucket")
    assert get_r2_bucket() == "real-bucket"


def test_get_r2_bucket_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # 빈 값 강제 — load_dotenv(override=False) 가 .env 파일로 덮어쓰지 못하게.
    monkeypatch.setenv("R2_BUCKET", "")
    with pytest.raises(R2NotConfigured):
        get_r2_bucket()


def test_get_r2_bucket_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_BUCKET", "your-r2-bucket")
    with pytest.raises(R2NotConfigured):
        get_r2_bucket()


def test_get_r2_client_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "")
    reset_client_cache()
    with pytest.raises(R2NotConfigured, match="R2_ACCESS_KEY_ID"):
        get_r2_client()


def test_get_r2_client_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ENDPOINT", "https://your-account-id.r2.cloudflarestorage.com")
    reset_client_cache()
    with pytest.raises(R2NotConfigured, match="placeholder"):
        get_r2_client()


def test_get_r2_client_returns_boto_s3_client() -> None:
    client = get_r2_client()
    assert client.meta.service_model.service_name == "s3"
    # path-style addressing 강제 확인 (R2 wildcard cert 매치용)
    assert client.meta.config.s3["addressing_style"] == "path"


def test_generate_presigned_put_url_returns_url() -> None:
    url = generate_presigned_put_url("clips/2026/05/27/p4cam-aabb/abc.mp4")
    assert url.startswith("https://test.r2.cloudflarestorage.com/")
    assert "X-Amz-Signature" in url
    assert "test-bucket" in url


def test_generate_presigned_get_url_returns_url() -> None:
    url = generate_presigned_get_url("clips/2026/05/27/p4cam-aabb/abc.mp4")
    assert url.startswith("https://test.r2.cloudflarestorage.com/")
    assert "X-Amz-Signature" in url


def test_delete_object_calls_client(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    class _FakeClient:
        def delete_object(self, **kwargs: object) -> None:
            called.update(kwargs)

    monkeypatch.setattr(r2_client, "get_r2_client", lambda: _FakeClient())
    r2_client.delete_object("clips/x.mp4")

    assert called["Bucket"] == "test-bucket"
    assert called["Key"] == "clips/x.mp4"
