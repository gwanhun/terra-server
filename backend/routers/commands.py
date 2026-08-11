"""
액추에이터 명령 라우터 — Stage H-1.

엔드포인트:
- POST /devices/{device_uuid}/mist   — 물분무 1/2/3초 (JWT)

## 왜 REST 엔드포인트? (앱이 commands 직접 INSERT 하는데)
mist 는 물이 나가는 액추에이터 → 서버측 검증(허용 지속시간)을 강제하고 싶다.
commands 직접 INSERT 는 payload 자유라 검증 지점이 없음. 이 경로로 들어오면
command_service 가 duration 을 화이트리스트로 clamp 한 뒤 pending INSERT.

발행은 기존 CommandDispatcher 담당. firmware 가 one-shot 타이머로 자동 OFF.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.command_service import (
    MIST_ACTION,
    InvalidCommand,
    insert_pending_command,
    validate_mist_duration,
)
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["commands"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}
_BAD_CMD = {400: {"description": "duration_ms 허용값 아님"}}


class MistRequest(BaseModel):
    duration_ms: int = Field(
        ...,
        description="분무 지속시간 (ms). 허용: 1000 | 2000 | 3000.",
        examples=[2000],
    )


class CommandOut(BaseModel):
    id: str = Field(..., description="commands.id — 앱이 Realtime 으로 status 추적")
    action: str
    status: str


def _load_device_for_owner(sb: Any, device_uuid: str, user_id: str) -> dict[str, Any]:
    """본인 소유 device row 반환. 미존재/타 유저는 404 (존재 여부 비노출).

    service_role 은 RLS 바이패스 → owner_id 명시 검증 필수 (CLAUDE.md 규칙).
    """
    res = (
        sb.table("devices")
        .select("id, owner_id")
        .eq("id", device_uuid)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="device not found")
    return row


@router.post(
    "/{device_uuid}/mist",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="물분무 (1/2/3초)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_BAD_CMD},
)
def mist(
    device_uuid: str,
    body: MistRequest,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    """물분무 명령 발행. firmware 가 duration 뒤 자동 OFF (단일 명령 자기완결).

    허용 지속시간(1000/2000/3000ms) 외는 400. 하드웨어(릴레이/MOSFET) 차이는
    firmware 가 흡수하므로 앱은 duration_ms 만 보내면 된다.
    """
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)

    try:
        duration = validate_mist_duration(body.duration_ms)
    except InvalidCommand as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inserted = insert_pending_command(
        sb,
        device_uuid=device_uuid,
        action=MIST_ACTION,
        payload={"duration_ms": duration},
        issued_by=user_id,
    )
    if inserted is None:
        raise HTTPException(status_code=500, detail="command INSERT 실패")

    return CommandOut(id=inserted["id"], action=MIST_ACTION, status="pending")
