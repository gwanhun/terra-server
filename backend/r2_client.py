"""
Cloudflare R2 (S3 호환) 클라이언트.

petcam-lab/backend/r2_uploader.py 패턴 차용. 차이점:
- terra-server 는 워커(ESP32-P4 / RPi)가 R2 에 직접 PUT 하는 구조라
  백엔드는 **presigned URL 발급만** 함 (upload_clip 같은 직접 PUT 헬퍼 없음).
- presigned **PUT** (워커→R2) + **GET** (앱→R2) 두 종류 모두 발급.

## 왜 boto3?
R2 는 AWS S3 API 호환. boto3 가 표준 SDK + moto 로 mocking 쉬움.

## 왜 lru_cache?
boto3.client(...) 는 HTTP 세션 생성 비용. 매 호출 새로 만들면 커넥션 폭증.
프로세스 스코프 싱글톤 (supabase_client 와 동일 패턴).

## R2 endpoint
대시보드 Account ID → `https://<account_id>.r2.cloudflarestorage.com`.
region 은 R2 표준값 `auto`.

## addressing_style="path" 강제
R2 wildcard cert (*.r2.cloudflarestorage.com) 는 한 단계만 매치.
기본 virtual-hosted 는 `<bucket>.<acc>.r2.cloudflarestorage.com` → SSL 실패.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

_PLACEHOLDER_PATTERNS = ("your-r2-", "PASTE_", "your-account-id")

# presigned URL TTL 기본값.
# PUT 은 5분 — 워커가 클립 인코딩 후 즉시 업로드. 길면 URL 유출 위험.
# GET 은 1시간 — 앱이 영상 재생 도중 만료 안 되게.
DEFAULT_PUT_URL_TTL = 300
DEFAULT_GET_URL_TTL = 3600


class R2NotConfigured(RuntimeError):
    """R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET 누락 또는 placeholder."""


@lru_cache(maxsize=1)
def get_r2_client() -> "S3Client":
    load_dotenv(REPO_ROOT / ".env")

    endpoint = os.getenv("R2_ENDPOINT")
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")

    missing = [
        name
        for name, val in (
            ("R2_ENDPOINT", endpoint),
            ("R2_ACCESS_KEY_ID", access_key),
            ("R2_SECRET_ACCESS_KEY", secret_key),
        )
        if not val
    ]
    if missing:
        raise R2NotConfigured(f"R2 환경변수 누락: {', '.join(missing)}. .env 확인.")

    for name, val in (
        ("R2_ENDPOINT", endpoint),
        ("R2_ACCESS_KEY_ID", access_key),
        ("R2_SECRET_ACCESS_KEY", secret_key),
    ):
        if val and any(p in val for p in _PLACEHOLDER_PATTERNS):
            raise R2NotConfigured(
                f"{name} 가 placeholder 상태. Cloudflare R2 대시보드 > "
                f"Manage R2 API Tokens 에서 실제 키 발급 후 .env 기입."
            )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def get_r2_bucket() -> str:
    load_dotenv(REPO_ROOT / ".env")
    bucket = os.getenv("R2_BUCKET")
    if not bucket or any(p in bucket for p in _PLACEHOLDER_PATTERNS):
        raise R2NotConfigured("R2_BUCKET 환경변수 누락 또는 placeholder.")
    return bucket


def generate_presigned_put_url(
    key: str,
    content_type: str = "video/mp4",
    expires_in: int = DEFAULT_PUT_URL_TTL,
) -> str:
    """워커가 R2 에 직접 PUT 할 URL. 호출 측이 동일 Content-Type 헤더로 PUT 해야 서명 일치."""
    client = get_r2_client()
    bucket = get_r2_bucket()

    return client.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def generate_presigned_get_url(
    key: str,
    expires_in: int = DEFAULT_GET_URL_TTL,
) -> str:
    """앱이 영상/썸네일 재생용으로 사용할 URL."""
    client = get_r2_client()
    bucket = get_r2_bucket()

    return client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def delete_object(key: str) -> None:
    """R2 에서 객체 영구 삭제. clips DELETE 라우터에서 호출."""
    client = get_r2_client()
    bucket = get_r2_bucket()
    client.delete_object(Bucket=bucket, Key=key)
    logger.info("r2 delete ok: bucket=%s key=%s", bucket, key)


def reset_client_cache() -> None:
    # monkeypatch 로 함수가 교체된 상태에서도 안전하게 no-op
    clear = getattr(get_r2_client, "cache_clear", None)
    if clear is not None:
        clear()


__all__ = [
    "DEFAULT_GET_URL_TTL",
    "DEFAULT_PUT_URL_TTL",
    "R2NotConfigured",
    "BotoCoreError",
    "ClientError",
    "delete_object",
    "generate_presigned_get_url",
    "generate_presigned_put_url",
    "get_r2_bucket",
    "get_r2_client",
    "reset_client_cache",
]
