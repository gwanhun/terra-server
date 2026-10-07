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
from uuid import UUID
import os
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend import ota_service
from backend.auth import get_current_user_id
from backend.crypto import generate_token, hash_token
from backend.mqtt import registry
from backend.mqtt.camera_commands import (
    ota_apply_command,
    ota_prepare_command,
    reboot_command,
    rotation_command,
)
from backend.supabase_client import get_supabase_client
from backend.unlink_service import unlink_entity
from backend.webrtc_signaling import MqttWebRTCSignaling

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cameras", tags=["cameras"])

_ALLOWED_MODELS = {"esp32-p4", "esp32-p4-hub", "rpi-zero-2-w", "rpi-4", "ip-camera"}
# esp32-p4-hub: 카메라+센서/액추에이터 통합 보드(Terra Hub). cameras 행 + devices 행을 함께 만든다.
HUB_MODEL = "esp32-p4-hub"
_ALLOWED_RESOLUTIONS = {"VGA", "HD", "FHD"}


# ---------- 소프트 해제 (앱 회신 2026-09-16 §1) ----------

class UnlinkRequest(BaseModel):
    request_id: UUID = Field(
        ..., description="앱 생성 UUID. 같은 값 재시도는 동일 응답(멱등)."
    )


class UnlinkResponse(BaseModel):
    id: str
    unlinked_at: str = Field(..., description="해제 시각(ISO8601). 재호출 시 최초 값 그대로")


class CameraPairRequest(BaseModel):
    enclosure_id: str | None = Field(
        None, description="소속 사육장 UUID. None 이면 단독 카메라."
    )
    name: str = Field(..., min_length=1, max_length=64, examples=["거실 카메라"])
    model: str = Field(
        default="esp32-p4",
        max_length=32,
        description="esp32-p4 | esp32-p4-hub (카메라+센서 통합) | rpi-zero-2-w | rpi-4 | ip-camera",
    )
    capabilities: dict[str, Any] | None = Field(
        None,
        description=(
            "esp32-p4-hub 전용. 기기(devices.capabilities) 능력 플래그 — "
            '{"board":"mosfet","led_dimmable":true,"hub":true,"mist_max_ms":30000}. '
            "순수 카메라는 무시."
        ),
    )
    firmware_ver: str | None = Field(None, max_length=64, examples=["terra-cam-p4 0.1.0"])
    resolution: str = Field(default="HD", description="VGA | HD (720p) | FHD (1080p)")
    fps: int = Field(default=24, ge=1, le=60)
    clip_sec: int = Field(default=10, ge=1, le=60, description="모션 감지 시 캡처 길이(초)")
    hw_id: str | None = Field(
        None,
        max_length=64,
        examples=["30EDA0E22E80"],
        description=(
            "보드 불변 하드웨어 ID(ESP32 efuse base MAC 12자리 hex). "
            "같은 owner 에 같은 hw_id 카메라가 이미 있으면 새 행을 만들지 않고 그 행을 "
            "재사용해 토큰만 재발급한다(재페어링 중복 행 방지). "
            "구 펌웨어는 보내지 않으며, 그 경우 기존대로 항상 새 행이 생긴다."
        ),
    )


class CameraPairResponse(BaseModel):
    reused: bool = Field(
        False,
        description=(
            "true 면 hw_id 가 일치하는 기존 카메라 행을 재사용했다(새 행 미생성). "
            "camera_id 는 종전 값 그대로이고 camera_token 만 새로 발급됐다."
        ),
    )
    id: str = Field(..., description="cameras.id (UUID)")
    camera_id: str = Field(
        ..., description="MQTT client_id 겸 username. 모델별 접두사 (p4cam-/picam-)"
    )
    camera_token: str = Field(
        ...,
        description=(
            "**평문 토큰. 응답에만 1회 노출.** NVS 에 저장 필수. 분실 시 재페어링. "
            "MQTT password 와 REST `Authorization: Bearer` 양쪽에 동일하게 사용."
        ),
    )
    mqtt_broker_host: str = Field(..., description="Mosquitto 브로커 호스트")
    mqtt_broker_port: int = Field(..., description="Mosquitto 브로커 포트 (TLS 8883 / 평문 1883)")
    mqtt_use_tls: bool = Field(..., description="true 면 TLS 8883 필수")
    device_uuid: str | None = Field(
        None,
        description=(
            "esp32-p4-hub 전용: 함께 만든 devices.id. 기기 API(/devices/{id}/…, 예약, 알림)는 이 "
            "UUID 로 호출한다. MQTT 토픽의 device_id 텍스트는 camera_id 와 같다."
        ),
    )


class CameraUpdate(BaseModel):
    enclosure_id: str | None = None
    name: str | None = Field(None, min_length=1, max_length=64)
    resolution: str | None = None
    fps: int | None = Field(None, ge=1, le=60)
    clip_sec: int | None = Field(None, ge=1, le=60)
    rotate_180: bool | None = Field(
        None,
        description=(
            "영상 180° 회전(설치 방향 보정). DB 갱신 즉시 200 응답, 카메라 적용은 비동기 "
            "(온라인이면 보통 1초 내, 오프라인이면 다음 재연결 시)."
        ),
    )


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
    rotate_180: bool = Field(False, description="영상 180° 회전 설정값 (선언적, 진실)")
    capabilities: dict[str, Any] | None = Field(
        None,
        description='펌웨어 보고 능력 플래그. 예 {"rotate_180": true}. null = 구 펌웨어(미보고)',
    )
    clip_stats: dict[str, Any] | None = Field(
        None,
        description=(
            "클립 파이프라인 카운터(부팅 후 누적, 15초 텔레메트리). rec/skip/skip_lock/up_ok/"
            "up_fail/sd_ok/sd_fail/sd_backlog, last_rec_s(마지막 녹화 후 초, -1=없음), "
            "up_busy_s(진행 중 업로드 초, -1=없음). null = 구 펌웨어"
        ),
    )
    clip_stats_at: str | None = None
    image_state: dict[str, Any] | None = Field(
        None,
        description="펌웨어 노출/야간 상태(15초 텔레메트리): exp(AE 목표 2~235), luma, chroma, night, ae_auto, ae_frozen. null = 구 펌웨어",
    )
    created_at: str
    updated_at: str
    last_seen_at: str | None
    is_online: bool

    model_config = ConfigDict(extra="ignore")


# GET /cameras · /cameras/{id} 가 읽는 컬럼 = CameraOut 필드 전부. 빠지면 에러 없이 모델
# 기본값(null/false)으로 나가 조용히 틀린다(2026-09-28: rotate_180 항상 false, clip_stats 항상 null).
# tests/test_cameras_api.py 가 CameraOut 과 어긋나지 않는지 검사한다.
_CAMERA_OUT_COLUMNS = (
    "id, camera_id, enclosure_id, name, model, firmware_ver, "
    "resolution, fps, clip_sec, stream_mode, stream_until, rotate_180, capabilities, "
    "clip_stats, clip_stats_at, image_state, "
    "created_at, updated_at, last_seen_at, is_online"
)


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


def _mqtt_connect_info() -> dict[str, Any]:
    """페어링 응답에 포함할 브로커 접속 정보. .env 의 MQTT_* 그대로 노출."""
    host = os.getenv("MQTT_BROKER_HOST", "").strip()
    if not host:
        raise HTTPException(
            status_code=500,
            detail="서버 설정 오류: MQTT_BROKER_HOST 가 비어있음.",
        )
    port_raw = os.getenv("MQTT_BROKER_PORT", "8883").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"서버 설정 오류: MQTT_BROKER_PORT 값 이상함 ({port_raw!r}).",
        ) from exc
    use_tls = os.getenv("MQTT_USE_TLS", "true").strip().lower() == "true"
    return {"mqtt_broker_host": host, "mqtt_broker_port": port, "mqtt_use_tls": use_tls}


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


def _find_by_hw_id(sb, user_id: str, hw_id: str | None) -> dict[str, Any] | None:
    """같은 소유자의 같은 물리 보드 카메라 행을 찾는다. 없으면 None.

    hw_id 는 efuse MAC 기반이라 재부팅·NVS 삭제·펌웨어 재설치에도 불변이다.
    unlinked_at IS NULL 로 제한해 해제된 과거 행은 되살리지 않는다(사용자가 의도적으로
    해제한 카메라를 페어링이 몰래 복구하면 안 된다 → 그 경우는 새 행이 맞다).
    구 펌웨어(hw_id 없음)는 항상 None → 기존 동작 그대로 새 행 생성.
    """
    if not hw_id:
        return None
    res = (
        sb.table("cameras")
        .select("id, camera_id, device_id")
        .eq("owner_id", user_id)
        .eq("hw_id", hw_id)
        .is_("unlinked_at", "null")
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None

def _ensure_hub_device(
    sb, user_id: str, body: CameraPairRequest, cam_row: dict[str, Any], token_hashed: str,
) -> str | None:
    """Terra Hub: cameras 행의 짝 devices 행을 만들거나 갱신하고 그 UUID 를 돌려준다.

    왜 두 행인가: 앱/웹/RLS/Realtime/예약/알림이 전부 devices 를 전제로 짜여 있다. 허브를
    cameras 행 하나로만 두면 센서 차트·예약·푸시가 전부 새 분기를 타야 한다. 대신 "한 보드 =
    두 행" 으로 두고 MQTT 계정을 공유한다: devices.device_id == cameras.camera_id (텍스트),
    token_hash 동일. 브리지는 payload 모양(dht22_a 유무)으로 어느 행을 갱신할지 가른다.

    재페어링(hw_id 재사용) 때는 cameras.device_id 링크로 기존 devices 행을 찾아 토큰만 갱신한다.
    링크가 비어 있지만 같은 device_id 텍스트의 행이 있으면(과거 수동 생성) 그 행을 재사용한다.
    실패는 500 — 허브가 카메라 행만 가지면 센서 데이터가 조용히 버려지므로 반쪽 성공을 막는다.
    """
    caps = body.capabilities or {"board": "mosfet", "led_dimmable": True, "hub": True}
    caps = {**caps, "hub": True}
    device_fields: dict[str, Any] = {
        "token_hash": token_hashed,
        "hw_id": body.hw_id,
        "firmware_ver": body.firmware_ver,
        "capabilities": caps,
    }
    if body.enclosure_id:
        device_fields["enclosure_id"] = body.enclosure_id

    linked = cam_row.get("device_id")
    if linked:
        res = sb.table("devices").update(device_fields).eq("id", linked).execute()
        if res.data:
            return linked
        logger.warning("hub pair: 링크된 devices 행 없음 (device_id=%s) — 새로 만든다", linked)

    res = (
        sb.table("devices")
        .select("id")
        .eq("device_id", cam_row["camera_id"])
        .limit(1)
        .execute()
    )
    if res.data:
        dev_uuid = res.data[0]["id"]
        sb.table("devices").update(device_fields).eq("id", dev_uuid).execute()
    else:
        payload: dict[str, Any] = {
            "owner_id": user_id,
            "enclosure_id": cam_row.get("enclosure_id") or body.enclosure_id,
            "device_id": cam_row["camera_id"],      # 같은 MQTT 계정 — 토픽 esp32/{id}/… 공유
            "name": cam_row.get("name") or body.name,
            **device_fields,
        }
        res = sb.table("devices").insert(payload).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="hub devices INSERT 실패")
        dev_uuid = res.data[0]["id"]

    link = sb.table("cameras").update({"device_id": dev_uuid}).eq("id", cam_row["id"]).execute()
    if not link.data:
        logger.warning("hub pair: cameras.device_id 링크 UPDATE 실패 (camera=%s)", cam_row["id"])
    return dev_uuid


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

    camera_token = generate_token()
    token_hashed = hash_token(camera_token)

    # 펌웨어가 보고한 하드웨어 ID 와 같은 보드가 이미 등록돼 있으면 새 행을 만들지 않는다.
    # (재페어링 = WiFi 변경일 뿐인데 매번 새 카메라가 생기던 문제. 2026-09-21)
    existing = _find_by_hw_id(sb, user_id, body.hw_id)

    # 기기가 보고하는 값만 갱신한다. 사용자가 서버/앱에서 바꾼 설정(rotate_180 등)이나
    # 미지정 enclosure_id 를 페어링이 덮어쓰지 않게 한다.
    device_fields: dict[str, Any] = {
        "model": body.model,
        "firmware_ver": body.firmware_ver,
        "resolution": body.resolution,
        "fps": body.fps,
        "clip_sec": body.clip_sec,
    }

    if existing:
        # name 은 재사용 시 건드리지 않는다(2026-09-23). 사용자가 앱에서 바꾼 이름과 petcam
        # 라벨링 트리거가 붙인 접미사(· 8636)를 WiFi 변경용 재페어링이 지워버렸다 — 펌웨어는
        # 프로비저닝 때 받은 이름을 매번 다시 보낸다. petcam 트리거는 행 생성 30분 뒤의 이름
        # 변경엔 접미사를 다시 붙이지 않으므로(v3) 서버가 안 쓰는 게 맞다. 이름 변경은 PATCH 로.
        patch: dict[str, Any] = {"token_hash": token_hashed, **device_fields}
        if body.enclosure_id:          # 미지정이면 기존 사육장 연결을 유지
            patch["enclosure_id"] = body.enclosure_id
        # 순수 카메라(p4cam-)로 등록됐던 보드를 허브 펌웨어로 리플래시한 경우: 같은 hw_id 라 행은
        # 재사용하되 camera_id 를 p4hub- 로 바꾼다. 브리지가 접두사로 조회 테이블을 고르므로
        # p4cam- 그대로면 센서 telemetry 가 cameras 경로로 빠져 조용히 버려진다. 펌웨어는 응답의
        # 새 camera_id/token 을 NVS 에 저장하고, 옛 MQTT 계정은 아래서 해지한다.
        old_camera_id: str | None = None
        if body.model == HUB_MODEL and not str(existing.get("camera_id", "")).startswith("p4hub-"):
            old_camera_id = existing.get("camera_id")
            patch["camera_id"] = f"p4hub-{secrets.token_hex(4)}"
        res = (
            sb.table("cameras")
            .update(patch)
            .eq("id", existing["id"])
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=500, detail="camera UPDATE 실패")
        row = res.data[0]
        if "camera_id" in patch:
            row["camera_id"] = patch["camera_id"]   # mock/부분 응답 대비 — 발급값이 진실
        if old_camera_id and old_camera_id != row["camera_id"]:
            registry.unregister_device(old_camera_id)
            logger.info("pair: 허브 전환 camera_id %s → %s (옛 MQTT 계정 해지)", old_camera_id, row["camera_id"])
        logger.info(
            "pair: hw_id=%s 기존 카메라 재사용 camera_id=%s (새 행 미생성)",
            body.hw_id, row["camera_id"],
        )
    else:
        # camera_id 접두사 — 모델별 구분 (운영 디버깅 편의). 브리지 _resolve_both 가 이 접두사로
        # 조회 테이블을 고르므로(p4hub = 둘 다) 바꾸면 handlers.py 도 같이 바꿔야 한다.
        prefix = {"esp32-p4": "p4cam", HUB_MODEL: "p4hub"}.get(body.model, "picam")
        payload: dict[str, Any] = {
            "owner_id": user_id,
            "enclosure_id": body.enclosure_id,
            "camera_id": f"{prefix}-{secrets.token_hex(4)}",
            "token_hash": token_hashed,
            "hw_id": body.hw_id,
            "name": body.name,          # 새 행에만 — 재사용 시엔 기존 이름 유지
            **device_fields,
        }
        res = sb.table("cameras").insert(payload).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="camera INSERT 실패")
        row = res.data[0]

    # Mosquitto 자동 등록 (실패해도 페어링 성공 처리).
    # 재사용 경로에서도 반드시 호출해야 한다 — 토큰을 새로 발급했으므로 브로커의
    # 기존 비밀번호로는 접속이 안 된다. mosquitto_passwd -b 는 같은 사용자면 덮어쓴다.
    # Terra Hub: 짝 devices 행(같은 device_id 텍스트·같은 토큰). 재페어링이면 토큰만 갱신.
    device_uuid: str | None = None
    if body.model == HUB_MODEL:
        if existing and "device_id" not in row:
            row["device_id"] = existing.get("device_id")
        device_uuid = _ensure_hub_device(sb, user_id, body, row, token_hashed)

    # Mosquitto 자동 등록 (실패해도 페어링 성공 처리). 허브도 계정은 하나(camera_id).
    registry.register_device(row["camera_id"], camera_token)

    return CameraPairResponse(
        id=row["id"],
        camera_id=row["camera_id"],
        camera_token=camera_token,
        reused=bool(existing),
        device_uuid=device_uuid,
        **_mqtt_connect_info(),
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
        .select(_CAMERA_OUT_COLUMNS)
        .eq("owner_id", user_id)
        .is_("unlinked_at", "null")            # 소프트 해제된 카메라는 제외 (앱 §1-3)
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
        .select(f"{_CAMERA_OUT_COLUMNS}, owner_id, unlinked_at")
        .eq("id", camera_uuid)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id or row.get("unlinked_at"):
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
        .is_("unlinked_at", "null")            # 해제된 카메라는 수정 불가 → 404
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="camera not found")
    cam = res.data[0]

    if "rotate_180" in updates and updates["rotate_180"] is not None:
        _publish_rotation(cam.get("camera_id", ""), bool(updates["rotate_180"]))

    return CameraOut.model_validate(cam)


def _publish_rotation(camera_id_text: str, rotate_180: bool) -> None:
    """set_rotation 명령 즉시 발행 (best-effort).

    DB 는 이미 갱신됐고, 발행이 실패해도 카메라 텔레메트리 동기화
    (backend/mqtt/handlers.py handle_telemetry camera 분기)가 DB 값으로 수렴시키므로
    5xx 를 던지지 않는다. MqttWebRTCSignaling 은 이름과 달리 "카메라 command 토픽
    one-shot publish" 이며 __init__ 이 env 누락 시 예외를 던지므로 통째로 감싼다.
    """
    if not camera_id_text:
        return
    try:
        MqttWebRTCSignaling().publish(camera_id_text, rotation_command(rotate_180))
        logger.info("set_rotation 발행 camera=%s rotate_180=%s", camera_id_text, rotate_180)
    except Exception:  # noqa: BLE001
        logger.warning(
            "set_rotation 발행 실패 camera=%s (텔레메트리 동기화로 수렴)",
            camera_id_text, exc_info=True,
        )


class CameraLogOut(BaseModel):
    id: int
    created_at: str
    uptime_s: int | None = None
    prev_boot: bool = False
    count: int = 1
    msg: str


@router.get(
    "/{camera_uuid}/logs",
    response_model=list[CameraLogOut],
    summary="카메라 펌웨어 에러 로그 (최근순)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def list_camera_logs(
    camera_uuid: str,
    limit: int = 100,
    user_id: str = Depends(get_current_user_id),
) -> list[CameraLogOut]:
    """하트비트 `errs` 로 올라온 ESP_LOGE 줄. prev_boot=true 는 재부팅 전 줄(원인 추적용)."""
    sb = get_supabase_client()
    cam = (
        sb.table("cameras").select("id, owner_id, unlinked_at")
        .eq("id", camera_uuid).single().execute()
    ).data
    if not cam or cam["owner_id"] != user_id or cam.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="camera not found")
    limit = max(1, min(int(limit), 500))
    res = (
        sb.table("camera_logs")
        .select("id, created_at, uptime_s, prev_boot, count, msg")
        .eq("camera_id", camera_uuid)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [CameraLogOut.model_validate(r) for r in (res.data or [])]


class RebootOut(BaseModel):
    published: bool = Field(..., description="MQTT command 발행 성공 여부(best-effort)")
    msg_id: str | None = Field(None, description="발행한 명령의 msg_id (ack 대조용)")


@router.post(
    "/{camera_uuid}/reboot",
    response_model=RebootOut,
    summary="카메라 원격 재부팅 (MQTT reboot 명령)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def reboot_camera(
    camera_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> RebootOut:
    """본인 카메라에 `reboot` 명령을 1회 발행한다(구 펌웨어는 rejected_unknown_action ack).

    하트비트는 살아 있는데 녹화·업로드가 멈춘 카메라용. 발행 실패는 5xx 대신
    published=false 로 알린다(브로커 일시 장애 시 앱이 재시도).
    """
    sb = get_supabase_client()
    res = (
        sb.table("cameras")
        .select("id, owner_id, camera_id, unlinked_at")
        .eq("id", camera_uuid)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id or row.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="camera not found")

    cmd = reboot_command()
    try:
        MqttWebRTCSignaling().publish(row.get("camera_id", ""), cmd)
        logger.info("reboot 발행 camera=%s msg_id=%s", row.get("camera_id"), cmd["msg_id"])
        return RebootOut(published=True, msg_id=cmd["msg_id"])
    except Exception:  # noqa: BLE001
        logger.warning("reboot 발행 실패 camera=%s", row.get("camera_id"), exc_info=True)
        return RebootOut(published=False, msg_id=None)


# ---------- OTA (Stage J, specs/stage-j-ota.md) ----------

class OtaRequest(BaseModel):
    release_id: str = Field(..., description="firmware_releases.id (target=camera_p4)")
    force: bool = Field(
        False,
        description="사전 점검 게이트(오프라인·약한 WiFi·내부 RAM·부팅 직후·업로드/라이브 중·"
                    "capabilities.ota 미보고)를 무시. 개발용 — 작업에 forced=true 로 남는다",
    )


class OtaJobOut(BaseModel):
    job_id: str
    status: str
    version: str
    published: bool = Field(..., description="MQTT 명령 발행 성공 여부(best-effort)")
    msg_id: str | None = None
    gate_reasons: list[str] = Field(default_factory=list, description="force 로 무시한 게이트 사유")


_OTA_CAMERA_COLUMNS = (
    "id, owner_id, camera_id, unlinked_at, firmware_ver, capabilities, clip_stats, "
    "last_seen_at, is_online, live_until"
)


def _load_camera_for_ota(sb, camera_uuid: str, user_id: str) -> dict[str, Any]:
    res = sb.table("cameras").select(_OTA_CAMERA_COLUMNS).eq("id", camera_uuid).limit(1).execute()
    row = (res.data or [None])[0]
    if not row or row.get("owner_id") != user_id or row.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="camera not found")
    return row


@router.post(
    "/{camera_uuid}/ota",
    response_model=OtaJobOut,
    status_code=status.HTTP_201_CREATED,
    summary="카메라 OTA 1단계: ota_prepare (다운로드·검증, 부팅 파티션 불변)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND,
               409: {"description": "진행 중 작업 있음 / 같은 버전 / 사전 점검 실패"}},
)
def start_camera_ota(
    camera_uuid: str,
    body: OtaRequest,
    user_id: str = Depends(get_current_user_id),
) -> OtaJobOut:
    """ota_jobs 생성 → `ota_prepare` 1회 발행. 펌웨어는 ack 후 재부팅해 깨끗한 상태에서 받는다.

    다운로드가 끝나면 작업이 `ready` 가 되고, 전환은 별도 `.../ota/{job_id}/apply` 로 보낸다.
    발행 실패는 작업을 failed(publish_failed) 로 닫고 published=false 로 알린다.
    """
    sb = get_supabase_client()
    cam = _load_camera_for_ota(sb, camera_uuid, user_id)
    gate = ota_service.gate_reasons(ota_service.KIND_CAMERA, cam) if body.force else []
    try:
        job, release, payload = ota_service.create_job(
            sb, kind=ota_service.KIND_CAMERA, entity=cam, release_id=body.release_id,
            issued_by=user_id, force=body.force,
        )
    except ota_service.OtaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    cmd = ota_prepare_command(
        job_id=payload["job_id"], version=payload["version"], size_bytes=payload["size"],
        sha256=payload["sha256"], url=payload["url"],
    )
    ota_service.set_prepare_msg(sb, job["id"], cmd["msg_id"])
    try:
        MqttWebRTCSignaling().publish(cam.get("camera_id", ""), cmd)
        logger.info("ota_prepare 발행 camera=%s job=%s msg_id=%s version=%s",
                    cam.get("camera_id"), job["id"], cmd["msg_id"], release["version"])
        return OtaJobOut(job_id=job["id"], status="pending", version=release["version"],
                         published=True, msg_id=cmd["msg_id"], gate_reasons=gate)
    except Exception:  # noqa: BLE001
        logger.warning("ota_prepare 발행 실패 camera=%s job=%s", cam.get("camera_id"), job["id"],
                       exc_info=True)
        ota_service.mark_failed(sb, job["id"], "publish_failed")
        return OtaJobOut(job_id=job["id"], status="failed", version=release["version"],
                         published=False, msg_id=None, gate_reasons=gate)


@router.post(
    "/{camera_uuid}/ota/{job_id}/apply",
    response_model=OtaJobOut,
    summary="카메라 OTA 2단계: ota_apply (부팅 파티션 전환 + 재부팅)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, 409: {"description": "작업이 ready 가 아님"}},
)
def apply_camera_ota(
    camera_uuid: str,
    job_id: str,
    user_id: str = Depends(get_current_user_id),
) -> OtaJobOut:
    """`ready` 작업에만 허용. 새 펌웨어의 heartbeat `fw` 가 릴리스 버전이면 verified,
    이전 버전으로 돌아오면 rolled_back, 10분 무소식이면 timeout (ota_service.OtaMonitor)."""
    sb = get_supabase_client()
    cam = _load_camera_for_ota(sb, camera_uuid, user_id)
    job = ota_service.get_job(sb, job_id)
    if not job or job["target_uuid"] != cam["id"]:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "ready":
        raise HTTPException(status_code=409, detail=f"job is {job['status']}, not ready")
    release = ota_service.get_release(sb, job["release_id"])
    version = release["version"] if release else ""

    cmd = ota_apply_command(job_id=job_id)
    try:
        MqttWebRTCSignaling().publish(cam.get("camera_id", ""), cmd)
    except Exception:  # noqa: BLE001
        logger.warning("ota_apply 발행 실패 camera=%s job=%s", cam.get("camera_id"), job_id,
                       exc_info=True)
        return OtaJobOut(job_id=job_id, status="ready", version=version, published=False, msg_id=None)
    ota_service.mark_applying(sb, job_id, cmd["msg_id"])
    logger.info("ota_apply 발행 camera=%s job=%s msg_id=%s", cam.get("camera_id"), job_id, cmd["msg_id"])
    return OtaJobOut(job_id=job_id, status="applying", version=version, published=True,
                     msg_id=cmd["msg_id"])


@router.post(
    "/{camera_uuid}/unlink",
    response_model=UnlinkResponse,
    summary="카메라 등록 해제 (소프트, 기록 보존)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def unlink_camera(
    camera_uuid: str,
    body: UnlinkRequest,
    user_id: str = Depends(get_current_user_id),
) -> UnlinkResponse:
    """행을 지우지 않고 `unlinked_at` 만 찍는다. `motion_clips` 와 R2 원본은 보존.

    클립 소유자는 촬영 시점 값이라 재등록한 새 소유자에게 과거 영상이 보이지 않는다.
    멱등·404 규칙은 devices 와 동일. 계약: docs/APP_DELIVERY_2026-09-16.md §1.3
    """
    sb = get_supabase_client()
    out = unlink_entity(
        sb, table="cameras", entity_uuid=camera_uuid,
        user_id=user_id, request_id=str(body.request_id),
    )
    return UnlinkResponse(**out)


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

    # Mosquitto password/ACL 제거
    registry.unregister_device(res.data[0]["camera_id"])
