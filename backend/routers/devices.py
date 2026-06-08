"""
디바이스 관리 라우터.

엔드포인트:
- POST /devices/pair       — 디바이스 페어링 (ESP32 ↔ 사용자 연결, 토큰 발급)
- GET  /devices            — 본인 디바이스 목록
- GET  /devices/{id}       — 단건 조회
- PATCH /devices/{id}      — 이름/종 수정
- DELETE /devices/{id}     — 디바이스 삭제

페어링 흐름:
1. ESP32 가 부팅 후 BLE 광고
2. 앱이 BLE 로 SSID/PW + 사용자 JWT + 디바이스 명/종 전달
3. ESP32 가 WiFi 연결 → 본 엔드포인트로 POST (JWT 헤더 포함)
4. 서버: JWT 검증 → device_id + 평문 토큰 생성 → bcrypt 해시 → devices INSERT
5. 응답으로 ESP32 에 평문 토큰 1회 전달 → ESP32 가 NVS 저장
6. ESP32 가 토큰으로 MQTT 브로커에 연결
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.crypto import generate_token, hash_token
from backend.mqtt import registry
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])


# ---------- Pydantic 모델 ----------

class DevicePairRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, examples=["거실 비어디드"])
    species: str | None = Field(None, max_length=32, examples=["bearded_dragon"])
    firmware_ver: str | None = Field(None, max_length=32, examples=["1.0.0"])


class DevicePairResponse(BaseModel):
    id: str = Field(..., description="devices.id (UUID)")
    device_id: str = Field(..., description="MQTT client_id (e.g. terra-a1b2c3d4)")
    mqtt_token: str = Field(
        ..., description="**MQTT password 평문. 응답에만 1회 노출.** NVS 저장 필수."
    )


class DeviceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    species: str | None = Field(None, max_length=32)


class DeviceOut(BaseModel):
    id: str = Field(..., description="UUID")
    device_id: str = Field(..., description="MQTT client_id")
    name: str
    species: str | None
    firmware_ver: str | None
    created_at: str
    last_seen_at: str | None
    is_online: bool


_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}


# ---------- 엔드포인트 ----------

@router.post(
    "/pair",
    response_model=DevicePairResponse,
    status_code=status.HTTP_201_CREATED,
    summary="신규 디바이스 페어링",
    responses={**_AUTH_REQUIRED},
)
def pair_device(
    body: DevicePairRequest,
    user_id: str = Depends(get_current_user_id),
) -> DevicePairResponse:
    """
    ESP32-S3 가 BLE 로 사용자 JWT 받고 WiFi 연결 직후 호출.

    응답의 `mqtt_token` 은 **단 1회만 노출** → ESP32 가 NVS 에 즉시 저장.
    이후 MQTT 브로커 연결 시 `username=device_id`, `password=mqtt_token` 으로 인증.
    """
    sb = get_supabase_client()

    device_id = f"terra-{secrets.token_hex(4)}"   # "terra-a1b2c3d4"
    mqtt_token = generate_token()
    token_hashed = hash_token(mqtt_token)

    payload: dict[str, Any] = {
        "owner_id": user_id,
        "device_id": device_id,
        "token_hash": token_hashed,
        "name": body.name,
        "species": body.species,
        "firmware_ver": body.firmware_ver,
    }

    res = sb.table("devices").insert(payload).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="device INSERT 실패")
    row = res.data[0]

    # Mosquitto 자동 등록 (실패해도 페어링 성공 처리 — 운영자가 수동 동기화 가능)
    registry.register_device(row["device_id"], mqtt_token)

    return DevicePairResponse(
        id=row["id"],
        device_id=row["device_id"],
        mqtt_token=mqtt_token,   # 평문은 응답에만 1회 노출
    )


@router.get(
    "",
    response_model=list[DeviceOut],
    summary="본인 디바이스 목록",
    responses={**_AUTH_REQUIRED},
)
def list_devices(
    user_id: str = Depends(get_current_user_id),
) -> list[DeviceOut]:
    """페어링 시각 내림차순. `token_hash` 는 응답에서 자동 제외."""
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select("id, device_id, name, species, firmware_ver, created_at, last_seen_at, is_online")
        .eq("owner_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [DeviceOut(**row) for row in (res.data or [])]


@router.get(
    "/{device_uuid}",
    response_model=DeviceOut,
    summary="디바이스 단건 조회",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def get_device(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> DeviceOut:
    """본인 디바이스가 아니면 404 (존재 여부 노출 안 함)."""
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select("id, device_id, name, species, firmware_ver, created_at, last_seen_at, is_online, owner_id")
        .eq("id", device_uuid)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="device not found")
    row.pop("owner_id", None)
    return DeviceOut(**row)


@router.patch(
    "/{device_uuid}",
    response_model=DeviceOut,
    summary="디바이스 수정 (이름/종)",
    responses={
        **_AUTH_REQUIRED,
        **_NOT_FOUND,
        400: {"description": "변경 필드 없음"},
    },
)
def update_device(
    device_uuid: str,
    body: DeviceUpdate,
    user_id: str = Depends(get_current_user_id),
) -> DeviceOut:
    """전송된 필드만 부분 업데이트 (exclude_unset)."""
    sb = get_supabase_client()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="변경 필드 없음")

    res = (
        sb.table("devices")
        .update(updates)
        .eq("id", device_uuid)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="device not found")
    row = res.data[0]
    row.pop("owner_id", None)
    row.pop("token_hash", None)
    return DeviceOut(**row)


@router.delete(
    "/{device_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="디바이스 삭제",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def delete_device(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> None:
    """cascade 로 `device_settings`, `telemetry`, `commands`, `alerts` 동시 삭제."""
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .delete()
        .eq("id", device_uuid)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="device not found")

    # Mosquitto password/ACL 제거 — 실패해도 DB 삭제는 성공 처리
    registry.unregister_device(res.data[0]["device_id"])
