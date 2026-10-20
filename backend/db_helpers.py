"""
사용자 입력 id 로 한 행 조회할 때 쓰는 헬퍼.

## 왜 `.single()` 을 안 쓰나?
실 PostgREST 는 `.single()` 결과가 0행이면 406(PGRST116) → postgrest APIError → 500.
앱은 5xx 를 재시도하므로 없는 id 에 무한 재시도가 된다. `.limit(1)` 리스트 조회로
0행 = 빈 리스트를 받아 각 자리의 not-found 응답(404/400/401)으로 돌린다.

## 왜 uuid 를 먼저 파싱하나?
id 컬럼이 UUID 라 형식이 틀린 문자열은 Postgres 캐스트 단계에서 22P02 → 500.
DB 에 보내기 전에 걸러서 not-found 로 처리한다. `str(uuid.UUID(...))` 로 정규화한
값을 이후 쿼리에 써야 한다 — `uuid.UUID` 는 `urn:uuid:...`·중괄호 형식도 받지만
Postgres 캐스트는 거부한다.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException


def parse_uuid(value: str | None) -> str | None:
    """정규화된 uuid 문자열. 형식이 아니면 None."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def parse_uuid_or_404(value: str, detail: str) -> str:
    """정규화된 uuid 문자열. 형식이 아니면 404(detail)."""
    parsed = parse_uuid(value)
    if parsed is None:
        raise HTTPException(status_code=404, detail=detail)
    return parsed


def first_row(data: Any) -> dict[str, Any] | None:
    """`.limit(1).execute().data` 의 첫 행. 0행이면 None."""
    return data[0] if data else None
