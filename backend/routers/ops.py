"""
운영 카메라 재부팅 라우터 (petcam-lab 요청 6, 2026-10-20, specs/stage-m-ops-camera-reboot.md).

petcam-lab 감시 스크립트가 밤 녹화가 2시간 멈춘 베타 카메라를 재부팅할 때 부른다.
요청서: petcam-lab/docs/requests/2026-10-20-terra-server-ops-camera-reboot.md

엔드포인트:
- POST /ops/cameras/{camera_uuid}/reboot — `X-Ops-Secret` 헤더(.env OPS_REBOOT_SECRET)

## 왜 사용자 JWT 가 아닌가?
감시 스크립트는 테스터 계정으로 로그인할 수 없고 그래서도 안 된다. service_role 키를 그대로
나누면 권한이 너무 넓다 → 이 경로 하나만 여는 서버 시크릿. 비교는 상수 시간(hmac.compare_digest).

## 안전장치 (검사 순서)
401 시크릿 → (카메라 락) 404 카메라 없음/해제 → 409 not_beta → offline → live_active → ota_active
→ 429 쿨다운 → 기록 INSERT → 발행 → 결과 UPDATE.

카메라 락은 live_session.claim 과 **같은 락**이다. 라이브 판정~발행 사이에 시청이 시작되는 창을
닫기 위해서다(락 안에서 카메라 행을 읽는다). 발행이 최대 5초라 그동안 같은 카메라 시청 시작이
기다릴 수 있다 — 밤·드문 호출이라 감수.

## 왜 기록을 먼저 INSERT 하나? (fail-closed)
INSERT 가 실패하면 발행하지 않는다(503). 마이그레이션 전 배포·Supabase 순단에도 쿨다운이
뚫리지 않고, 카메라 ack 가 INSERT 보다 먼저 와서 기록이 빠지는 경합도 없다.

## 앱 알림 없음
push_events·알림 outbox 를 부르지 않는다. 앱은 사용자가 누른 재시작에만 반응한다(회신서 참고).
"""

from __future__ import annotations

import hmac
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.db_helpers import first_row, parse_uuid_or_404
from backend.live_session import _lock_for as camera_lock
from backend.live_session import _ts as parse_ts
from backend.mqtt.camera_commands import reboot_command
from backend.ota_service import active_job_for
from backend.routers.cameras import RebootOut
from backend.supabase_client import get_supabase_client
from backend.webrtc_signaling import MqttWebRTCSignaling

logger = logging.getLogger(__name__)

OPS_REBOOT_COOLDOWN_SEC = 2 * 60 * 60
BETA_GROUP = "beta"
_LOG_TABLE = "camera_ops_reboots"
_CAMERA_COLUMNS = "id, camera_id, labeling_group, unlinked_at, is_online, live_session_id, stream_until"


def require_ops_secret(x_ops_secret: str | None = Header(None, alias="X-Ops-Secret")) -> None:
    """서버 시크릿이 비었거나 미설정이면 헤더와 무관하게 401(빈 값끼리 일치 금지). 값은 로그 금지."""
    expected = os.getenv("OPS_REBOOT_SECRET", "").strip()   # push_events 시크릿과 같이 공백·개행 무시
    given = x_ops_secret or ""
    ok = hmac.compare_digest(given.encode(), expected.encode())
    if not (expected and given and ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid ops secret")


router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_ops_secret)])


class OpsRebootIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=64, description="예: night_rec_stall")
    requested_by: str = Field(..., min_length=1, max_length=64, description="예: beta-camera-watch")


def _skipped(reason: str) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                        content={"detail": "skipped", "reason": reason})


def _skip_reason(sb: Any, cam: dict[str, Any], now: datetime) -> str | None:
    """재부팅하지 않을 이유. 없으면 None."""
    if cam.get("labeling_group") != BETA_GROUP:
        return "not_beta"
    if not cam.get("is_online"):
        return "offline"
    stream_until = parse_ts(cam.get("stream_until"))
    if cam.get("live_session_id") or (stream_until is not None and stream_until > now):
        return "live_active"
    if active_job_for(sb, cam["id"]) is not None:
        return "ota_active"
    return None


def _cooldown_retry_after(sb: Any, cam_uuid: str, now: datetime) -> int | None:
    """2시간 안에 published 가 false 가 아닌(성공 + 진행 중) 기록이 있으면 남은 초. 없으면 None.

    개수 제한 없이 창 전체를 본다 — 성공 뒤에 실패가 여러 건 쌓여도 성공 행을 놓치지 않게.
    """
    since = (now - timedelta(seconds=OPS_REBOOT_COOLDOWN_SEC)).isoformat()
    rows = (
        sb.table(_LOG_TABLE)
        .select("requested_at, published")
        .eq("camera_id", cam_uuid)
        .gte("requested_at", since)
        .order("requested_at", desc=True)
        .execute()
    ).data or []
    for row in rows:
        if row.get("published") is False:
            continue   # 발행 실패는 쿨다운에 안 셈 — 다음 실행에서 재시도
        at = parse_ts(row.get("requested_at"))
        if at is None:
            continue
        return max(1, math.ceil(OPS_REBOOT_COOLDOWN_SEC - (now - at).total_seconds()))
    return None


def _set_published(sb: Any, msg_id: str, published: bool) -> None:
    try:
        sb.table(_LOG_TABLE).update({"published": published}).eq("msg_id", msg_id).execute()
    except Exception:  # noqa: BLE001 — 행은 published=NULL 로 남아 쿨다운에 계속 포함(안전 쪽)
        logger.error("ops reboot 결과 기록 실패 msg_id=%s published=%s", msg_id, published,
                     exc_info=True)


@router.post(
    "/cameras/{camera_uuid}/reboot",
    response_model=RebootOut,
    summary="운영 카메라 재부팅 (서버 시크릿, 베타 카메라 한정)",
    responses={
        401: {"description": "시크릿 없음/불일치 (서버 미설정 포함)"},
        404: {"description": "카메라 없음/해제됨"},
        409: {"description": "skipped — reason: not_beta | offline | live_active | ota_active"},
        429: {"description": "cooldown — retry_after 초 (Retry-After 헤더 동일)"},
        503: {"description": "ops log unavailable — 기록 실패, 재부팅 안 함"},
    },
)
def ops_reboot_camera(camera_uuid: str, body: OpsRebootIn) -> RebootOut | JSONResponse:
    """베타 카메라에 `reboot` 명령을 1회 발행하고 기록한다. 앱에는 알리지 않는다."""
    cam_uuid = parse_uuid_or_404(camera_uuid, "camera not found")
    sb = get_supabase_client()
    with camera_lock(cam_uuid):
        cam = first_row((
            sb.table("cameras").select(_CAMERA_COLUMNS).eq("id", cam_uuid).limit(1).execute()
        ).data)
        if not cam or cam.get("unlinked_at"):
            raise HTTPException(status_code=404, detail="camera not found")

        now = datetime.now(timezone.utc)
        reason = _skip_reason(sb, cam, now)
        if reason:
            logger.info("ops reboot 건너뜀 camera=%s reason=%s", cam.get("camera_id"), reason)
            return _skipped(reason)

        retry_after = _cooldown_retry_after(sb, cam_uuid, now)
        if retry_after is not None:
            return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                content={"detail": "cooldown", "retry_after": retry_after},
                                headers={"Retry-After": str(retry_after)})

        cmd = reboot_command()
        try:
            sb.table(_LOG_TABLE).insert({
                "camera_id": cam_uuid,
                "msg_id": cmd["msg_id"],
                "reason": body.reason,
                "requested_by": body.requested_by,
                "requested_at": now.isoformat(),
                "published": None,
            }).execute()
        except Exception:  # noqa: BLE001 — 기록 못 하면 발행 안 함(fail-closed)
            logger.error("ops reboot 기록 실패 → 발행 안 함 camera=%s", cam.get("camera_id"),
                         exc_info=True)
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                content={"detail": "ops log unavailable"})

        try:
            MqttWebRTCSignaling().publish(cam.get("camera_id", ""), cmd)
        except Exception:  # noqa: BLE001
            logger.warning("ops reboot 발행 실패 camera=%s msg_id=%s", cam.get("camera_id"),
                           cmd["msg_id"], exc_info=True)
            _set_published(sb, cmd["msg_id"], False)
            return RebootOut(published=False, msg_id=None)

        logger.info("ops reboot 발행 camera=%s msg_id=%s reason=%s by=%s", cam.get("camera_id"),
                    cmd["msg_id"], body.reason, body.requested_by)
        _set_published(sb, cmd["msg_id"], True)
        return RebootOut(published=True, msg_id=cmd["msg_id"])
