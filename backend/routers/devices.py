"""
디바이스 관리 라우터.

엔드포인트:
- POST /devices/pair       — 디바이스 페어링 (ESP32 ↔ 사용자 연결, 토큰 발급, enclosure 배정 옵션)
- GET  /devices            — 본인 디바이스 목록
- GET  /devices/{id}       — 단건 조회
- PATCH /devices/{id}      — 이름/종/사육장(enclosure_id) 수정
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
from uuid import UUID
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.crypto import generate_token, hash_token
from backend.mqtt import registry
from backend.supabase_client import get_supabase_client
from backend.unlink_service import unlink_entity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])


# ---------- Pydantic 모델 ----------

# 펌웨어가 보고하지 않을 때의 기본 보드 능력 (MOSFET 4채널 보드, 조명 밝기 조절 가능).
# 릴레이 보드용 기기를 웹에서 등록하려면 body.capabilities 로 명시할 것.
DEFAULT_CAPABILITIES: dict[str, Any] = {"board": "mosfet", "led_dimmable": True}


class DevicePairRequest(BaseModel):
    enclosure_id: str | None = Field(
        None, description="소속 사육장 UUID. None 이면 단독 디바이스."
    )
    name: str = Field(..., min_length=1, max_length=64, examples=["거실 비어디드"])
    species: str | None = Field(None, max_length=32, examples=["bearded_dragon"])
    firmware_ver: str | None = Field(None, max_length=32, examples=["1.0.0"])
    capabilities: dict[str, Any] | None = Field(
        None,
        description='보드 능력 플래그(펌웨어 보고). 예: {"board":"mosfet","led_dimmable":true}',
        examples=[{"board": "mosfet", "led_dimmable": True, "heater": True}],
    )
    hw_id: str | None = Field(
        None,
        max_length=64,
        examples=["A0B7651C2908"],
        description=(
            "보드 불변 하드웨어 ID(ESP32 efuse base MAC 12자리 hex). "
            "같은 owner 에 같은 hw_id 기기가 이미 있으면 새 행을 만들지 않고 그 행을 "
            "재사용해 토큰만 재발급한다(재페어링 중복 행 방지). "
            "구 펌웨어는 보내지 않으며, 그 경우 기존대로 항상 새 행이 생긴다."
        ),
    )


# ---------- 소프트 해제 (앱 회신 2026-09-16 §1) ----------

class UnlinkRequest(BaseModel):
    request_id: UUID = Field(
        ..., description="앱 생성 UUID. 같은 값 재시도는 동일 응답(멱등)."
    )


class UnlinkResponse(BaseModel):
    id: str
    unlinked_at: str = Field(..., description="해제 시각(ISO8601). 재호출 시 최초 값 그대로")


class DevicePairResponse(BaseModel):
    id: str = Field(..., description="devices.id (UUID)")
    device_id: str = Field(..., description="MQTT client_id (e.g. terra-a1b2c3d4)")
    mqtt_token: str = Field(
        ..., description="**MQTT password 평문. 응답에만 1회 노출.** NVS 저장 필수."
    )
    reused: bool = Field(
        False,
        description=(
            "true 면 hw_id 가 일치하는 기존 기기 행을 재사용했다(새 행 미생성). "
            "device_id 는 종전 값 그대로이고 mqtt_token 만 새로 발급됐다."
        ),
    )


class DeviceUpdate(BaseModel):
    enclosure_id: str | None = None
    name: str | None = Field(None, min_length=1, max_length=64)
    species: str | None = Field(None, max_length=32)


class DeviceOut(BaseModel):
    id: str = Field(..., description="UUID")
    device_id: str = Field(..., description="MQTT client_id")
    enclosure_id: str | None = None
    name: str
    species: str | None
    firmware_ver: str | None
    capabilities: dict[str, Any] | None = None
    created_at: str
    last_seen_at: str | None
    is_online: bool


_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}
_BAD_ENCLOSURE = {400: {"description": "enclosure_id 가 본인 사육장이 아님"}}


def _verify_enclosure_owner(sb, enclosure_id: str, user_id: str) -> None:
    """enclosure_id 가 본인 소유 사육장인지 확인. 아니면 400.

    cameras 라우터와 동일 패턴 (service_role 은 RLS 바이패스 → 명시 검증 필수).
    """
    res = (
        sb.table("enclosures")
        .select("owner_id")
        .eq("id", enclosure_id)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=400, detail="enclosure_id 가 본인 사육장이 아님.")


def _find_by_hw_id(sb, user_id: str, hw_id: str | None) -> dict[str, Any] | None:
    """같은 소유자의 같은 물리 보드 기기 행을 찾는다. 없으면 None.

    hw_id 는 efuse MAC 기반이라 재부팅·NVS 삭제·펌웨어 재설치에도 불변이다.
    unlinked_at IS NULL 로 제한해 해제된 과거 행은 되살리지 않는다(사용자가 의도적으로
    해제한 기기를 페어링이 몰래 복구하면 안 된다 → 그 경우는 새 행이 맞다).
    구 펌웨어(hw_id 없음)는 항상 None → 기존 동작 그대로 새 행 생성.
    """
    if not hw_id:
        return None
    res = (
        sb.table("devices")
        .select("id, device_id")
        .eq("owner_id", user_id)
        .eq("hw_id", hw_id)
        .is_("unlinked_at", "null")
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


# ---------- 엔드포인트 ----------

@router.post(
    "/pair",
    response_model=DevicePairResponse,
    status_code=status.HTTP_201_CREATED,
    summary="신규 디바이스 페어링",
    responses={**_AUTH_REQUIRED, **_BAD_ENCLOSURE},
)
def pair_device(
    body: DevicePairRequest,
    user_id: str = Depends(get_current_user_id),
) -> DevicePairResponse:
    """
    ESP32-S3 가 BLE 로 사용자 JWT 받고 WiFi 연결 직후 호출.

    응답의 `mqtt_token` 은 **단 1회만 노출** → ESP32 가 NVS 에 즉시 저장.
    이후 MQTT 브로커 연결 시 `username=device_id`, `password=mqtt_token` 으로 인증.

    `enclosure_id` 가 본인 소유가 아니면 400.
    """
    sb = get_supabase_client()

    if body.enclosure_id:
        _verify_enclosure_owner(sb, body.enclosure_id, user_id)

    mqtt_token = generate_token()
    token_hashed = hash_token(mqtt_token)

    # 펌웨어가 보고한 하드웨어 ID 와 같은 보드가 이미 등록돼 있으면 새 행을 만들지 않는다.
    # (재페어링 = WiFi 변경일 뿐인데 매번 새 기기가 생기던 문제. 2026-09-21, cameras 와 동일)
    existing = _find_by_hw_id(sb, user_id, body.hw_id)

    # capabilities 미지정(웹 등록 패널 등 펌웨어 보고가 없는 경로)이면 기본 보드 플래그.
    # null 이면 앱이 밝기 슬라이더를 숨긴다 (2026-09-18 베타기기 2908 사례).
    # 펌웨어는 pair body 로 명시 보고하므로 그 값이 우선.
    caps = body.capabilities if body.capabilities is not None else dict(DEFAULT_CAPABILITIES)

    # 기기가 보고하는 값만 갱신한다. 사용자가 서버/앱에서 바꾼 설정이나 미지정
    # enclosure_id 를 페어링이 덮어쓰지 않게 한다.
    device_fields: dict[str, Any] = {
        "firmware_ver": body.firmware_ver,
        "capabilities": caps,
    }

    if existing:
        # name/species 는 재사용 시 건드리지 않는다(2026-09-23, cameras 와 동일). 펌웨어는
        # 프로비저닝 때 받은 이름을 매번 다시 보내므로, 사용자가 앱에서 바꾼 이름·종을
        # WiFi 변경용 재페어링이 지워버린다. 변경은 PATCH /devices/{id} 로.
        patch: dict[str, Any] = {"token_hash": token_hashed, **device_fields}
        if body.enclosure_id:          # 미지정이면 기존 사육장 연결을 유지
            patch["enclosure_id"] = body.enclosure_id
        res = (
            sb.table("devices")
            .update(patch)
            .eq("id", existing["id"])
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=500, detail="device UPDATE 실패")
        row = res.data[0]
        logger.info(
            "pair: hw_id=%s 기존 디바이스 재사용 device_id=%s (새 행 미생성)",
            body.hw_id, row["device_id"],
        )
    else:
        payload: dict[str, Any] = {
            "owner_id": user_id,
            "enclosure_id": body.enclosure_id,
            "device_id": f"terra-{secrets.token_hex(4)}",   # "terra-a1b2c3d4"
            "token_hash": token_hashed,
            "hw_id": body.hw_id,
            "name": body.name,          # 새 행에만 — 재사용 시엔 기존 이름·종 유지
            "species": body.species,
            **device_fields,
        }
        res = sb.table("devices").insert(payload).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="device INSERT 실패")
        row = res.data[0]

    # Mosquitto 자동 등록 (실패해도 페어링 성공 처리 — 운영자가 수동 동기화 가능).
    # 재사용 경로에서도 반드시 호출해야 한다 — 토큰을 새로 발급했으므로 브로커의
    # 기존 비밀번호로는 접속이 안 된다. mosquitto_passwd -b 는 같은 사용자면 덮어쓴다.
    registry.register_device(row["device_id"], mqtt_token)

    return DevicePairResponse(
        id=row["id"],
        device_id=row["device_id"],
        mqtt_token=mqtt_token,   # 평문은 응답에만 1회 노출
        reused=bool(existing),
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
    """페어링 시각 내림차순. 소프트 해제된 기기는 제외. `token_hash` 는 응답에서 자동 제외."""
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select(
            "id, device_id, enclosure_id, name, species, firmware_ver, "
            "capabilities, created_at, last_seen_at, is_online"
        )
        .eq("owner_id", user_id)
        .is_("unlinked_at", "null")            # 소프트 해제된 기기는 제외 (앱 §1-3)
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
    """본인 디바이스가 아니거나 소프트 해제됐으면 404 (존재 여부 노출 안 함)."""
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select(
            "id, device_id, enclosure_id, name, species, firmware_ver, "
            "capabilities, created_at, last_seen_at, is_online, owner_id, unlinked_at"
        )
        .eq("id", device_uuid)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id or row.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="device not found")
    row.pop("owner_id", None)
    row.pop("unlinked_at", None)
    return DeviceOut(**row)


@router.patch(
    "/{device_uuid}",
    response_model=DeviceOut,
    summary="디바이스 수정 (이름/종/enclosure_id)",
    responses={
        **_AUTH_REQUIRED,
        **_NOT_FOUND,
        **_BAD_ENCLOSURE,
        400: {"description": "변경 필드 없음"},
    },
)
def update_device(
    device_uuid: str,
    body: DeviceUpdate,
    user_id: str = Depends(get_current_user_id),
) -> DeviceOut:
    """전송된 필드만 부분 업데이트 (exclude_unset).

    `enclosure_id` 로 사육장에 배정/해제 (None 이면 단독 디바이스로 분리).
    본인 소유 사육장이 아니면 400.
    """
    sb = get_supabase_client()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="변경 필드 없음")

    if "enclosure_id" in updates and updates["enclosure_id"] is not None:
        _verify_enclosure_owner(sb, updates["enclosure_id"], user_id)

    res = (
        sb.table("devices")
        .update(updates)
        .eq("id", device_uuid)
        .eq("owner_id", user_id)
        .is_("unlinked_at", "null")            # 해제된 기기는 수정 불가 → 404
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="device not found")
    row = res.data[0]
    row.pop("owner_id", None)
    row.pop("token_hash", None)
    return DeviceOut(**row)


@router.post(
    "/{device_uuid}/unlink",
    response_model=UnlinkResponse,
    summary="디바이스 등록 해제 (소프트, 기록 보존)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def unlink_device(
    device_uuid: str,
    body: UnlinkRequest,
    user_id: str = Depends(get_current_user_id),
) -> UnlinkResponse:
    """행을 지우지 않고 `unlinked_at` 만 찍는다. 앱의 "기기 삭제" 는 이걸 쓴다.

    - 보존: 행, telemetry*, commands, alerts, schedules(비활성화), motion_clips, R2
    - 해제 시: enclosure_id=NULL, schedules.enabled=false, MQTT 계정 회수
    - 멱등: 같은 request_id 재시도 / 이미 해제된 기기 → 200 + 기존 unlinked_at
    - 이후 GET/PATCH 는 404, 목록에서 제외. hard delete(DELETE) 는 운영용으로 별도.

    계약: docs/APP_DELIVERY_2026-09-16.md §1.3 · backend/unlink_service.py
    """
    sb = get_supabase_client()
    out = unlink_entity(
        sb, table="devices", entity_uuid=device_uuid,
        user_id=user_id, request_id=str(body.request_id),
    )
    return UnlinkResponse(**out)


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
    """**hard delete (운영·탈퇴용).** 앱의 "등록 해제" 는 `POST /devices/{id}/unlink` 를 쓴다.

    동반 삭제: `device_settings`, `telemetry`, `telemetry_1m`, `telemetry_30m`(장기 통계),
    `commands`, `alerts`, `schedules`.

    기록을 남기는 해제는 미구현 (docs/BACKEND_HANDOFF_REPLY_REDESIGN_2026-09-15.md §2).
    """
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
