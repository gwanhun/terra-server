"""
카메라 워커 관리 라우터.

엔드포인트:
- POST   /cameras/pair         — 페어링 (BLE 흐름) + camera_token 발급 (JWT)
- GET    /cameras              — 본인 카메라 목록 (JWT)
- GET    /cameras/{id}         — 단건 (JWT)
- PATCH  /cameras/{id}         — 수정 (JWT)
- DELETE /cameras/{id}         — 삭제 (JWT)

페어링 흐름 ([specs/stage-f-camera-ingest.md](../../specs/stage-f-camera-ingest.md)):
1. ESP32-P4 BLE 광고 (Terra-Cam-XXXX)
2. 앱이 BLE 로 SSID/PW + JWT + name/model/enclosure_id 전달
3. ESP32-P4 → WiFi 연결 → POST /cameras/pair (JWT)
4. 서버: camera_id + camera_token 생성 → bcrypt 해시 → INSERT
5. 평문 camera_token 응답에 1회 노출 → NVS 저장
6. 워커가 MQTT 연결 + clips/snapshot/webrtc 호출 시 Bearer 인증

devices/pair 와 동일 패턴. 차이: enclosure_id 옵션, model/resolution/fps/clip_sec.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import get_current_user_id
from backend.crypto import generate_token, hash_token
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cameras", tags=["cameras"])

_ALLOWED_MODELS = {"esp32-p4", "rpi-zero-2-w", "rpi-4", "ip-camera"}
_ALLOWED_RESOLUTIONS = {"VGA", "HD", "FHD"}


class CameraPairRequest(BaseModel):
    enclosure_id: str | None = Field(
        None, description="소속 사육장 UUID. None 이면 단독 카메라."
    )
    name: str = Field(..., min_length=1, max_length=64, examples=["거실 카메라"])
    model: str = Field(
        default="esp32-p4",
        max_length=32,
        description="esp32-p4 | rpi-zero-2-w | rpi-4 | ip-camera",
    )
    firmware_ver: str | None = Field(None, max_length=64, examples=["terra-cam-p4 0.1.0"])
    resolution: str = Field(default="HD", description="VGA | HD (720p) | FHD (1080p)")
    fps: int = Field(default=24, ge=1, le=60)
    clip_sec: int = Field(default=10, ge=1, le=60, description="모션 감지 시 캡처 길이(초)")


class CameraPairResponse(BaseModel):
    id: str = Field(..., description="cameras.id (UUID)")
    camera_id: str = Field(
        ..., description="MQTT client_id. 모델별 접두사 (p4cam-/picam-)"
    )
    camera_token: str = Field(
        ...,
        description=(
            "**평문 토큰. 응답에만 1회 노출.** NVS 에 저장 필수. 분실 시 재페어링."
        ),
    )


class CameraUpdate(BaseModel):
    enclosure_id: str | None = None
    name: str | None = Field(None, min_length=1, max_length=64)
    resolution: str | None = None
    fps: int | None = Field(None, ge=1, le=60)
    clip_sec: int | None = Field(None, ge=1, le=60)


class CameraOut(BaseModel):
    id: str = Field(..., description="UUID")
    camera_id: str = Field(..., description="MQTT client_id")
    enclosure_id: str | None
    name: str
    model: str | None
    firmware_ver: str | None
    resolution: str | None
    fps: int | None
    clip_sec: int | None
    stream_mode: str | None = Field(None, description="NULL | snapshot | webrtc (Stage G)")
    stream_until: str | None
    created_at: str
    updated_at: str
    last_seen_at: str | None
    is_online: bool

    model_config = ConfigDict(extra="ignore")


_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 카메라가 아니거나 미존재"}}
_BAD_ENUM = {400: {"description": "model/resolution enum 위반 또는 enclosure_id 권한 없음"}}


def _validate_enums(model: str | None, resolution: str | None) -> None:
    if model is not None and model not in _ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model 은 {sorted(_ALLOWED_MODELS)} 중 하나여야 함.",
        )
    if resolution is not None and resolution not in _ALLOWED_RESOLUTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"resolution 은 {sorted(_ALLOWED_RESOLUTIONS)} 중 하나여야 함.",
        )


def _verify_enclosure_owner(sb, enclosure_id: str, user_id: str) -> None:
    """enclosure_id 가 본인 소유 사육장인지 확인. 아니면 400."""
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


@router.post(
    "/pair",
    response_model=CameraPairResponse,
    status_code=status.HTTP_201_CREATED,
    summary="신규 카메라 워커 페어링",
    responses={**_AUTH_REQUIRED, **_BAD_ENUM},
)
def pair_camera(
    body: CameraPairRequest,
    user_id: str = Depends(get_current_user_id),
) -> CameraPairResponse:
    """
    BLE 페어링 후 워커가 호출. JWT 로 사용자 식별.

    응답의 `camera_token` 평문은 **단 1회만 노출**되니 워커가 NVS 에 즉시 저장해야 한다.
    이후 `/cameras/{id}/clips/*`, `/snapshot`, `/webrtc/*` 호출 시 Bearer 인증에 사용.

    `enclosure_id` 가 본인 소유가 아니면 400. model/resolution enum 위반도 400.
    """
    _validate_enums(body.model, body.resolution)

    sb = get_supabase_client()

    if body.enclosure_id:
        _verify_enclosure_owner(sb, body.enclosure_id, user_id)

    # camera_id 접두사 — 모델별 구분 (운영 디버깅 편의)
    prefix = "p4cam" if body.model == "esp32-p4" else "picam"
    camera_id = f"{prefix}-{secrets.token_hex(4)}"

    camera_token = generate_token()
    token_hashed = hash_token(camera_token)

    payload: dict[str, Any] = {
        "owner_id": user_id,
        "enclosure_id": body.enclosure_id,
        "camera_id": camera_id,
        "token_hash": token_hashed,
        "name": body.name,
        "model": body.model,
        "firmware_ver": body.firmware_ver,
        "resolution": body.resolution,
        "fps": body.fps,
        "clip_sec": body.clip_sec,
    }

    res = sb.table("cameras").insert(payload).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="camera INSERT 실패")
    row = res.data[0]

    return CameraPairResponse(
        id=row["id"],
        camera_id=row["camera_id"],
        camera_token=camera_token,
    )


@router.get(
    "",
    response_model=list[CameraOut],
    summary="본인 카메라 목록",
    responses={**_AUTH_REQUIRED},
)
def list_cameras(
    user_id: str = Depends(get_current_user_id),
) -> list[CameraOut]:
    """생성 시각 내림차순. `token_hash` 는 응답에서 자동 제외."""
    sb = get_supabase_client()
    res = (
        sb.table("cameras")
        .select(
            "id, camera_id, enclosure_id, name, model, firmware_ver, "
            "resolution, fps, clip_sec, stream_mode, stream_until, "
            "created_at, updated_at, last_seen_at, is_online"
        )
        .eq("owner_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [CameraOut.model_validate(r) for r in (res.data or [])]


@router.get(
    "/{camera_uuid}",
    response_model=CameraOut,
    summary="카메라 단건 조회",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def get_camera(
    camera_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> CameraOut:
    """본인 카메라가 아니면 404."""
    sb = get_supabase_client()
    res = (
        sb.table("cameras")
        .select(
            "id, owner_id, camera_id, enclosure_id, name, model, firmware_ver, "
            "resolution, fps, clip_sec, stream_mode, stream_until, "
            "created_at, updated_at, last_seen_at, is_online"
        )
        .eq("id", camera_uuid)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="camera not found")
    return CameraOut.model_validate(row)


@router.patch(
    "/{camera_uuid}",
    response_model=CameraOut,
    summary="카메라 수정 (이름/해상도/fps/clip_sec/enclosure_id)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_BAD_ENUM},
)
def update_camera(
    camera_uuid: str,
    body: CameraUpdate,
    user_id: str = Depends(get_current_user_id),
) -> CameraOut:
    """전송된 필드만 부분 업데이트. resolution 변경은 다음 캡처부터 적용."""
    _validate_enums(None, body.resolution)

    sb = get_supabase_client()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="변경 필드 없음")

    if "enclosure_id" in updates and updates["enclosure_id"] is not None:
        _verify_enclosure_owner(sb, updates["enclosure_id"], user_id)

    res = (
        sb.table("cameras")
        .update(updates)
        .eq("id", camera_uuid)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="camera not found")
    return CameraOut.model_validate(res.data[0])


@router.delete(
    "/{camera_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="카메라 삭제",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def delete_camera(
    camera_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> None:
    """소속 `motion_clips` 도 cascade 삭제. R2 객체는 30일 lifecycle 로 자동 정리."""
    sb = get_supabase_client()
    res = (
        sb.table("cameras")
        .delete()
        .eq("id", camera_uuid)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="camera not found")
