"""
LCD 커스텀 텍스트 라우터 — Stage I.

엔드포인트:
- POST /devices/{device_uuid}/lcd        — 상단 밴드에 커스텀 텍스트 표시 (JWT)
- POST /devices/{device_uuid}/lcd/clear  — 기본값("TERRA IOT")으로 복귀 (JWT)

흐름: 서버가 텍스트를 1비트 비트맵으로 렌더 → `commands`(action='lcd_bitmap') 큐잉 →
dispatcher publish → 펌웨어가 base64 디코드 후 상단 밴드에 blit + NVS 저장(재부팅 유지).
mist 와 동일 파이프라인 재사용.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.command_service import insert_pending_command
from backend.lcd_render import MAX_TEXT_LEN, build_lcd_payload
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["lcd"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}

# LCD 명령 TTL — 비트맵 payload 라 발행 지연 여유를 좀 더 준다.
LCD_TTL_SEC = 30


class LcdTextRequest(BaseModel):
    text: str = Field(
        ...,
        max_length=MAX_TEXT_LEN,
        description=f"표시할 텍스트(한글 가능). 최대 {MAX_TEXT_LEN}자. 빈 문자열이면 기본값 복귀.",
        examples=["밥 6시"],
    )


class CommandOut(BaseModel):
    id: str
    action: str
    status: str


def _load_device_for_owner(sb: Any, device_uuid: str, user_id: str) -> None:
    res = (
        sb.table("devices").select("id, owner_id").eq("id", device_uuid).limit(1).execute()
    )
    row = (res.data or [None])[0]
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="device not found")


@router.post(
    "/{device_uuid}/lcd",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 커스텀 텍스트 표시",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def set_lcd_text(
    device_uuid: str,
    body: LcdTextRequest,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    """텍스트를 서버에서 비트맵으로 렌더 → 디바이스 상단 밴드에 표시. 빈 문자열이면 clear 처리."""
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)

    if not body.text.strip():
        # 빈 텍스트 → 기본값 복귀
        return _insert(sb, device_uuid, user_id, "lcd_clear", None)

    payload = build_lcd_payload(body.text)
    return _insert(sb, device_uuid, user_id, "lcd_bitmap", payload)


@router.post(
    "/{device_uuid}/lcd/clear",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 텍스트 초기화 (기본값 복귀)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def clear_lcd_text(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)
    return _insert(sb, device_uuid, user_id, "lcd_clear", None)


def _insert(
    sb: Any, device_uuid: str, user_id: str, action: str, payload: dict | None
) -> CommandOut:
    inserted = insert_pending_command(
        sb,
        device_uuid=device_uuid,
        action=action,
        payload=payload,
        issued_by=user_id,
        ttl_sec=LCD_TTL_SEC,
    )
    if inserted is None:
        raise HTTPException(status_code=500, detail="command INSERT 실패")
    return CommandOut(id=inserted["id"], action=action, status="pending")
