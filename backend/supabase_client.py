"""
Supabase 싱글톤 클라이언트 (service_role).

petcam-lab/backend/supabase_client.py 패턴 그대로.

## 왜 service_role?
백엔드는 모든 디바이스의 telemetry/commands/alerts 를 INSERT/UPDATE 해야 하니
RLS 바이패스 가능한 service_role 키 필요. 절대 클라이언트(앱) 에 노출 금지.

앱(Flutter/웹) 은 anon 키 + JWT → RLS 가 본인 디바이스만 노출.

## 왜 lru_cache?
create_client 는 HTTP 세션 생성 비용. 매 요청마다 새로 만들면 커넥션 폭증.
프로세스 스코프 싱글톤으로 재사용.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

REPO_ROOT = Path(__file__).resolve().parent.parent

_PLACEHOLDER_PATTERNS = ("PASTE_", "your-service-role-key", "your-project")


class SupabaseNotConfigured(RuntimeError):
    """SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 누락 또는 placeholder."""


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    load_dotenv(REPO_ROOT / ".env")

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise SupabaseNotConfigured(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 환경변수 필요. .env 확인."
        )
    if any(p in url for p in _PLACEHOLDER_PATTERNS) or any(
        p in key for p in _PLACEHOLDER_PATTERNS
    ):
        raise SupabaseNotConfigured(
            "SUPABASE_URL / SERVICE_ROLE_KEY 가 placeholder 상태. "
            "Supabase 대시보드 > Settings > API 에서 실제 값 복사."
        )

    return create_client(url, key)


def reset_client_cache() -> None:
    get_supabase_client.cache_clear()
