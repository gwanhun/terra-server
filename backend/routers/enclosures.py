"""
사육장(enclosure) 관리 라우터.

enclosure 는 디바이스/카메라의 상위 묶음. 한 사육장 = 1 ESP32-S3 + N 카메라 같은 단위.
단독 디바이스/카메라(enclosure 없이)도 허용 → 모든 관계는 ON DELETE SET NULL.

엔드포인트:
- POST   /enclosures           — 생성
- GET    /enclosures           — 본인 사육장 목록
- GET    /enclosures/{id}      — 단건
- PATCH  /enclosures/{id}      — 수정
- DELETE /enclosures/{id}      — 삭제
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import get_current_user_id
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enclosures", tags=["enclosures"])


class EnclosureCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, examples=["거실 사육장"])
    species: str | None = Field(None, max_length=32, examples=["bearded_dragon"])
    note: str | None = Field(None, max_length=500, examples=["온도 떨어지면 알림"])


class EnclosureUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    species: str | None = Field(None, max_length=32)
    note: str | None = Field(None, max_length=500)


class EnclosureOut(BaseModel):
    id: str = Field(..., description="UUID")
    name: str
    species: str | None
    note: str | None
    created_at: str = Field(..., description="ISO8601")
    updated_at: str = Field(..., description="ISO8601")

    model_config = ConfigDict(extra="ignore")


_NOT_FOUND = {404: {"description": "본인 사육장이 아니거나 미존재"}}
_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}


@router.post(
    "",
    response_model=EnclosureOut,
    status_code=status.HTTP_201_CREATED,
    summary="사육장 생성",
    responses={**_AUTH_REQUIRED},
)
def create_enclosure(
    body: EnclosureCreate,
    user_id: str = Depends(get_current_user_id),
) -> EnclosureOut:
    """사육장(enclosure) 신규 등록. 본인 소유 자동 설정."""
    sb = get_supabase_client()
    payload: dict[str, Any] = {
        "owner_id": user_id,
        "name": body.name,
        "species": body.species,
        "note": body.note,
    }
    res = sb.table("enclosures").insert(payload).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="enclosure INSERT 실패")
    return EnclosureOut.model_validate(res.data[0])


@router.get(
    "",
    response_model=list[EnclosureOut],
    summary="본인 사육장 목록",
    responses={**_AUTH_REQUIRED},
)
def list_enclosures(
    user_id: str = Depends(get_current_user_id),
) -> list[EnclosureOut]:
    """생성 시각 내림차순. 페이지네이션 없음 (사용자당 사육장 수가 적다고 가정)."""
    sb = get_supabase_client()
    res = (
        sb.table("enclosures")
        .select("id, name, species, note, created_at, updated_at")
        .eq("owner_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [EnclosureOut.model_validate(r) for r in (res.data or [])]


@router.get(
    "/{enclosure_id}",
    response_model=EnclosureOut,
    summary="사육장 단건 조회",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def get_enclosure(
    enclosure_id: str,
    user_id: str = Depends(get_current_user_id),
) -> EnclosureOut:
    """본인 소유가 아니면 404 (존재 여부 노출 안 함)."""
    sb = get_supabase_client()
    res = (
        sb.table("enclosures")
        .select("id, owner_id, name, species, note, created_at, updated_at")
        .eq("id", enclosure_id)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="enclosure not found")
    return EnclosureOut.model_validate(row)


@router.patch(
    "/{enclosure_id}",
    response_model=EnclosureOut,
    summary="사육장 수정",
    responses={
        **_AUTH_REQUIRED,
        **_NOT_FOUND,
        400: {"description": "변경 필드 없음"},
    },
)
def update_enclosure(
    enclosure_id: str,
    body: EnclosureUpdate,
    user_id: str = Depends(get_current_user_id),
) -> EnclosureOut:
    """전송된 필드만 부분 업데이트 (exclude_unset). 빈 body 는 400."""
    sb = get_supabase_client()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="변경 필드 없음")

    res = (
        sb.table("enclosures")
        .update(updates)
        .eq("id", enclosure_id)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="enclosure not found")
    return EnclosureOut.model_validate(res.data[0])


@router.delete(
    "/{enclosure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="사육장 삭제",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def delete_enclosure(
    enclosure_id: str,
    user_id: str = Depends(get_current_user_id),
) -> None:
    """소속된 device/camera/clip 의 `enclosure_id` 는 NULL 로 설정 (cascade 아님)."""
    sb = get_supabase_client()
    res = (
        sb.table("enclosures")
        .delete()
        .eq("id", enclosure_id)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="enclosure not found")
