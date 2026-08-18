"""디바이스 설정(목표 환경) 라우터 — 앱 핸드오프 §5.

엔드포인트:
- GET   /devices/{device_uuid}/settings  — 목표 온/습도 조회 (JWT)
- PATCH /devices/{device_uuid}/settings  — 목표 온/습도 수정 (JWT)

## 왜 REST 인가
Supabase RLS 직결로도 device_settings 를 쓸 수 있지만, 값 범위 검증(온도 -20~60,
습도 0~100, min<=max)을 서버 한 곳에서 하려고 REST 를 둔다. 유지형 가드가 이 목표값을
기준으로 동작할 예정이라 잘못된 setpoint 가 저장되면 위험.

## 저장 방식
device_settings PK = device_id (= devices.id). 디바이스당 1행이라 upsert.
service_role 은 RLS 바이패스되므로 소유권을 **명시적으로** 검증한다(디바이스 owner 확인).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.alerts import reset_settings_cache
from backend.auth import get_current_user_id
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["settings"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}
_BAD = {400: {"description": "값 범위/일관성 검증 실패"}}

# setpoint 열만 앱에 노출 (alert_*·schedule 은 별도 관심사).
_SETTINGS_COLS = (
    "device_id, target_temp_c, target_humidity_pct, "
    "target_temp_min, target_temp_max, target_humid_min, target_humid_max, updated_at"
)

# 물리적으로 말이 되는 범위 (사육장 일반 범위보다 넉넉하게).
_TEMP_RANGE = (-20.0, 60.0)
_HUMID_RANGE = (0.0, 100.0)


class SettingsUpdate(BaseModel):
    target_temp_c: float | None = Field(None, description="목표 온도 단일값 (°C)")
    target_humidity_pct: float | None = Field(None, description="목표 습도 단일값 (%RH)")
    target_temp_min: float | None = None
    target_temp_max: float | None = None
    target_humid_min: float | None = None
    target_humid_max: float | None = None


class SettingsOut(BaseModel):
    device_id: str
    target_temp_c: float | None = None
    target_humidity_pct: float | None = None
    target_temp_min: float | None = None
    target_temp_max: float | None = None
    target_humid_min: float | None = None
    target_humid_max: float | None = None
    updated_at: str | None = None

    model_config = ConfigDict(extra="ignore")


def _load_device_for_owner(sb: Any, device_uuid: str, user_id: str) -> None:
    """디바이스가 본인 소유인지 확인. 아니면 404 (존재 여부 노출 안 함)."""
    res = (
        sb.table("devices").select("id, owner_id").eq("id", device_uuid).limit(1).execute()
    )
    row = (res.data or [None])[0]
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="device not found")


def _in_range(name: str, val: float | None, lo: float, hi: float) -> None:
    if val is not None and not (lo <= val <= hi):
        raise HTTPException(status_code=400, detail=f"{name} 는 {lo}~{hi} 범위여야 함")


def _validate(updates: dict[str, Any], existing: dict[str, Any]) -> None:
    """범위 + min<=max 검증. 부분 수정이라 기존값과 병합해서 판정."""
    _in_range("target_temp_c", updates.get("target_temp_c"), *_TEMP_RANGE)
    _in_range("target_temp_min", updates.get("target_temp_min"), *_TEMP_RANGE)
    _in_range("target_temp_max", updates.get("target_temp_max"), *_TEMP_RANGE)
    _in_range("target_humidity_pct", updates.get("target_humidity_pct"), *_HUMID_RANGE)
    _in_range("target_humid_min", updates.get("target_humid_min"), *_HUMID_RANGE)
    _in_range("target_humid_max", updates.get("target_humid_max"), *_HUMID_RANGE)

    def merged(key: str) -> float | None:
        return updates[key] if key in updates else existing.get(key)

    t_min, t_max = merged("target_temp_min"), merged("target_temp_max")
    if t_min is not None and t_max is not None and t_min > t_max:
        raise HTTPException(status_code=400, detail="target_temp_min 은 target_temp_max 이하여야 함")
    h_min, h_max = merged("target_humid_min"), merged("target_humid_max")
    if h_min is not None and h_max is not None and h_min > h_max:
        raise HTTPException(status_code=400, detail="target_humid_min 은 target_humid_max 이하여야 함")


def _fetch_settings(sb: Any, device_uuid: str) -> dict[str, Any] | None:
    res = (
        sb.table("device_settings")
        .select(_SETTINGS_COLS)
        .eq("device_id", device_uuid)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


@router.get(
    "/{device_uuid}/settings",
    response_model=SettingsOut,
    summary="디바이스 목표 환경 조회",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def get_settings(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> SettingsOut:
    """미설정이면 값이 모두 null 인 빈 설정을 반환 (404 아님)."""
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)
    row = _fetch_settings(sb, device_uuid)
    if not row:
        return SettingsOut(device_id=device_uuid)
    return SettingsOut(**row)


@router.patch(
    "/{device_uuid}/settings",
    response_model=SettingsOut,
    summary="디바이스 목표 환경 수정 (upsert)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_BAD},
)
def update_settings(
    device_uuid: str,
    body: SettingsUpdate,
    user_id: str = Depends(get_current_user_id),
) -> SettingsOut:
    """전송된 필드만 부분 수정. 행 없으면 생성(upsert)."""
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="변경 필드 없음")

    existing = _fetch_settings(sb, device_uuid) or {}
    _validate(updates, existing)

    updates["device_id"] = device_uuid
    res = sb.table("device_settings").upsert(updates, on_conflict="device_id").execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="device_settings upsert 실패")

    reset_settings_cache()  # alerts.py 설정 캐시 무효화
    row = _fetch_settings(sb, device_uuid) or res.data[0]
    return SettingsOut(**row)
