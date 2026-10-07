"""
액추에이터 명령 라우터 — Stage H-1.

엔드포인트:
- POST /devices/{device_uuid}/mist   — 물분무 1/2/3초 (JWT)
- POST /devices/{device_uuid}/reboot — 원격 재부팅 (JWT, 2026-09-28)

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

from backend import ota_service
from backend.auth import get_current_user_id
from backend.device_access import require_active_device
from backend.command_service import (
    MIST_ACTION,
    InvalidCommand,
    insert_pending_command,
    validate_mist_duration,
)
from backend.mqtt.camera_commands import ACTION_OTA_APPLY, ACTION_OTA_PREPARE, OTA_TTL_SEC
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["commands"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}
_BAD_CMD = {400: {"description": "duration_ms 허용값 아님"}}


class MistRequest(BaseModel):
    duration_ms: int = Field(
        ...,
        description="분무 지속시간 (ms). 1000~20000 범위의 정수(2026-09-28 화이트리스트 폐지). "
                    "기기 상한(capabilities.mist_max_ms, 미보고=5000)을 넘으면 서버가 5초 단위로 나눠 보낸다.",
        examples=[2000],
    )


class CommandOut(BaseModel):
    id: str = Field(..., description="commands.id — 앱이 Realtime 으로 status 추적")
    action: str
    status: str


# 원격 재부팅. 펌웨어(command_dispatch.c `reboot`)는 ack 를 먼저 보내고 1.5초 뒤 재부팅하며,
# 다음 telemetry 의 reset 이 "SW:mqtt_reboot" 로 보고된다(devices.sys_state.reset).
# 카메라 reboot 과 달리 commands 테이블을 거치므로 ack/no_ack 가 기록된다.
REBOOT_ACTION = "reboot"
REBOOT_TTL_SEC = 60




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
    require_active_device(sb, device_uuid, user_id)

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


@router.post(
    "/{device_uuid}/reboot",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="IoT 기기 원격 재부팅 (MQTT reboot 명령)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def reboot(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    """본인 기기에 `reboot` 명령 1건을 큐잉한다. 구 펌웨어는 `unknown_action` ack.

    액추에이터는 부팅 초기 블록이 전부 OFF 로 잡으므로 분무 중이어도 안전하다.
    결과는 commands.status(acked / no_ack) 와 다음 telemetry 의 sys_state.reset 으로 확인.
    """
    sb = get_supabase_client()
    require_active_device(sb, device_uuid, user_id)

    inserted = insert_pending_command(
        sb,
        device_uuid=device_uuid,
        action=REBOOT_ACTION,
        payload=None,
        issued_by=user_id,
        ttl_sec=REBOOT_TTL_SEC,
    )
    if inserted is None:
        raise HTTPException(status_code=500, detail="command INSERT 실패")

    logger.info("reboot 큐잉 device=%s command_id=%s", device_uuid, inserted["id"])
    return CommandOut(id=inserted["id"], action=REBOOT_ACTION, status="pending")


# ---------- OTA (Stage J, specs/stage-j-ota.md) — nano 만, supermini 제외 ----------
#
# 카메라와 달리 commands 테이블을 거친다: dispatcher 가 발행하고 ack/no_ack/expired 가 기록된다.
# ota_jobs 는 commands.id 를 prepare_msg_id / apply_msg_id 로 들고 ack 의 `ota` 블록을 받는다.

class OtaRequest(BaseModel):
    release_id: str = Field(..., description="firmware_releases.id (target=device_nano)")
    force: bool = Field(False, description="사전 점검 게이트 무시(개발용). 작업에 forced=true 로 남음")


class OtaJobOut(BaseModel):
    job_id: str
    status: str
    version: str
    command_id: str = Field(..., description="commands.id — 앱/콘솔이 Realtime 으로 ack 추적")
    gate_reasons: list[str] = Field(default_factory=list)


_OTA_DEVICE_COLUMNS = (
    "id, owner_id, device_id, unlinked_at, firmware_ver, capabilities, sys_state, "
    "last_seen_at, is_online"
)


def _load_device_for_ota(sb: Any, device_uuid: str, user_id: str) -> dict[str, Any]:
    res = sb.table("devices").select(_OTA_DEVICE_COLUMNS).eq("id", device_uuid).limit(1).execute()
    row = (res.data or [None])[0]
    if not row or row.get("owner_id") != user_id or row.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="device not found")
    return row


@router.post(
    "/{device_uuid}/ota",
    response_model=OtaJobOut,
    status_code=status.HTTP_201_CREATED,
    summary="기기 OTA 1단계: ota_prepare 큐잉 (다운로드·검증, 부팅 파티션 불변)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND,
               409: {"description": "진행 중 작업 있음 / 같은 버전 / 사전 점검 실패"}},
)
def start_device_ota(
    device_uuid: str,
    body: OtaRequest,
    user_id: str = Depends(get_current_user_id),
) -> OtaJobOut:
    sb = get_supabase_client()
    dev = _load_device_for_ota(sb, device_uuid, user_id)
    gate = ota_service.gate_reasons(ota_service.KIND_DEVICE, dev) if body.force else []
    try:
        job, release, payload = ota_service.create_job(
            sb, kind=ota_service.KIND_DEVICE, entity=dev, release_id=body.release_id,
            issued_by=user_id, force=body.force,
        )
    except ota_service.OtaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    inserted = insert_pending_command(
        sb, device_uuid=device_uuid, action=ACTION_OTA_PREPARE, payload=payload,
        issued_by=user_id, ttl_sec=OTA_TTL_SEC, source="manual", reason="ota_prepare",
    )
    if inserted is None:
        ota_service.mark_failed(sb, job["id"], "command_insert_failed")
        raise HTTPException(status_code=500, detail="command INSERT 실패")
    ota_service.set_prepare_msg(sb, job["id"], inserted["id"])
    logger.info("ota_prepare 큐잉 device=%s job=%s command=%s version=%s",
                device_uuid, job["id"], inserted["id"], release["version"])
    return OtaJobOut(job_id=job["id"], status="pending", version=release["version"],
                     command_id=inserted["id"], gate_reasons=gate)


@router.post(
    "/{device_uuid}/ota/{job_id}/apply",
    response_model=OtaJobOut,
    summary="기기 OTA 2단계: ota_apply 큐잉 (부팅 파티션 전환 + 재부팅)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, 409: {"description": "작업이 ready 가 아님"}},
)
def apply_device_ota(
    device_uuid: str,
    job_id: str,
    user_id: str = Depends(get_current_user_id),
) -> OtaJobOut:
    sb = get_supabase_client()
    dev = _load_device_for_ota(sb, device_uuid, user_id)
    job = ota_service.get_job(sb, job_id)
    if not job or job["target_uuid"] != dev["id"]:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "ready":
        raise HTTPException(status_code=409, detail=f"job is {job['status']}, not ready")
    release = ota_service.get_release(sb, job["release_id"])
    version = release["version"] if release else ""

    inserted = insert_pending_command(
        sb, device_uuid=device_uuid, action=ACTION_OTA_APPLY, payload={"job_id": job_id},
        issued_by=user_id, ttl_sec=OTA_TTL_SEC, source="manual", reason="ota_apply",
    )
    if inserted is None:
        raise HTTPException(status_code=500, detail="command INSERT 실패")
    ota_service.mark_applying(sb, job_id, inserted["id"])
    logger.info("ota_apply 큐잉 device=%s job=%s command=%s", device_uuid, job_id, inserted["id"])
    return OtaJobOut(job_id=job_id, status="applying", version=version, command_id=inserted["id"])
