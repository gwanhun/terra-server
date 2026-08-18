"""
예약 타이머 라우터 — Stage H-2.

엔드포인트:
- POST   /devices/{device_uuid}/schedules   — 예약 생성 (JWT)
- GET    /devices/{device_uuid}/schedules   — 디바이스 예약 목록 (JWT)
- PATCH  /schedules/{schedule_id}            — 예약 수정 (JWT)
- DELETE /schedules/{schedule_id}            — 예약 삭제 (JWT)

## 흐름
앱이 예약 등록 → next_run_at(UTC) 을 서버가 계산해서 저장 → schedule_runner 가
due 시점에 commands 로 발행. 즉 이 라우터는 "언제 무슨 명령을 낼지"만 관리.

## 라우터 2개
prefix 가 /devices 와 /schedules 로 달라 인스턴스 분리 (clips 라우터와 동일 패턴).
main.py 에서 둘 다 include.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import get_current_user_id
from backend.command_service import ALLOWED_MIST_MS, MIST_ACTION
from backend.scheduling import compute_next_run, parse_time_of_day
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

device_schedules_router = APIRouter(prefix="/devices", tags=["schedules"])
schedules_router = APIRouter(prefix="/schedules", tags=["schedules"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 리소스가 아니거나 미존재"}}
_BAD = {400: {"description": "검증 실패 (action/payload/time_of_day/요일)"}}

# 예약 가능 action 화이트리스트. mist 는 payload 추가 검증.
# on/off 계열(절대 상태·멱등)만 허용 — 무인 실행에서 상태가 확정돼야 안전하다.
# toggle(relay/fan/heater/led)은 기기 현재 상태를 뒤집는데, 예약은 무인이라 그 시점
# 상태를 아무도 못 봐 어긋나면(예: 이미 켜진 히터를 또 toggle→OFF 대신 유지) 과열 위험 →
# 화이트리스트에서 제외해 예약 생성/수정 시 400 거부 (앱 요청 §6). 펌웨어는 여전히 지원하나
# 예약 경로만 막는다. 즉시 제어(commands 직접 발행)에는 toggle 사용 가능.
SCHEDULABLE_ACTIONS: frozenset[str] = frozenset({
    MIST_ACTION,
    "relay_on", "relay_off",
    "fan_on", "fan_off",
    "heater_on", "heater_off",
    "led_on", "led_off",
})

# 스마트 조건(guard) 타입. skip_when_* 는 서버(schedule_runner)가 발행 직전 평가.
# stop_when_* 는 펌웨어가 가동 중 처리 — 서버는 저장만(발행은 정상).
GUARD_TYPES: frozenset[str] = frozenset({
    "skip_when_humidity_above", "skip_when_humidity_below",
    "skip_when_temp_above", "skip_when_temp_below",
    "stop_when_temp_above", "stop_when_humidity_below",
})


# ---------- Pydantic ----------

class ScheduleCreate(BaseModel):
    action: str = Field(..., examples=[MIST_ACTION])
    payload: dict[str, Any] | None = Field(
        None, description="action 인자. mist 는 {\"duration_ms\": 2000} 필수."
    )
    kind: Literal["daily", "weekly"]
    time_of_day: str = Field(..., description="KST 실행 시각 'HH:MM'", examples=["08:00"])
    days_of_week: list[int] | None = Field(
        None, description="weekly 전용. 1=월 .. 7=일. 최소 1개.", examples=[[1, 3, 5]]
    )
    guard: dict[str, Any] | None = Field(
        None,
        description='스마트 조건. 예: {"type":"skip_when_humidity_above","value":70,"enabled":true}',
    )
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    payload: dict[str, Any] | None = None
    kind: Literal["daily", "weekly"] | None = None
    time_of_day: str | None = None
    days_of_week: list[int] | None = None
    guard: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduleOut(BaseModel):
    id: str
    device_id: str
    action: str
    payload: dict[str, Any] | None
    kind: str
    time_of_day: str
    days_of_week: list[int] | None
    guard: dict[str, Any] | None = None
    enabled: bool
    next_run_at: str
    last_run_at: str | None
    created_at: str

    model_config = ConfigDict(extra="ignore")


# ---------- 헬퍼 ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_action_payload(action: str, payload: dict[str, Any] | None) -> None:
    """action 화이트리스트 + mist payload 검증. 실패 시 400."""
    if action not in SCHEDULABLE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"예약 불가 action: {action!r} (허용: {sorted(SCHEDULABLE_ACTIONS)})",
        )
    if action == MIST_ACTION:
        dur = (payload or {}).get("duration_ms")
        if dur not in ALLOWED_MIST_MS:
            raise HTTPException(
                status_code=400,
                detail=f"mist payload.duration_ms 는 {ALLOWED_MIST_MS} 중 하나여야 함",
            )


def _validate_guard(guard: dict[str, Any] | None, action: str | None = None) -> None:
    """guard 검증 (있을 때만). type 화이트리스트 + value 숫자. 실패 시 400.

    off 계열 action(`*_off`)에는 guard 부착을 금지한다 (앱 요청 §4-3, 히터 안전):
    off 예약에 skip 가드가 걸리면 억제 조건 성립 시 off 가 스킵돼 기기가 켜진 채
    남는다(예: 히터 과열). on/mist 예약에만 가드가 의미 있으므로 서버에서 거부해
    웹 콘솔 등 다른 클라이언트 경로까지 안전하게 막는다.
    """
    if guard is None:
        return
    if guard.get("type") not in GUARD_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"guard.type 는 {sorted(GUARD_TYPES)} 중 하나여야 함",
        )
    if not isinstance(guard.get("value"), (int, float)) or isinstance(guard.get("value"), bool):
        raise HTTPException(status_code=400, detail="guard.value 는 숫자여야 함")
    if action is not None and action.endswith("_off"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"off action({action!r})에는 guard 를 걸 수 없음 — "
                "가드로 off 가 스킵되면 기기가 켜진 채 남아 위험하다. "
                "가드는 on/mist 예약에만 사용."
            ),
        )


def _validate_days(kind: str, days: list[int] | None) -> None:
    """weekly 는 요일 1개 이상 + 각 값 1..7. daily 는 검증 없음(무시)."""
    if kind != "weekly":
        return
    if not days:
        raise HTTPException(status_code=400, detail="weekly 는 days_of_week 최소 1개 필요")
    if any(d < 1 or d > 7 for d in days):
        raise HTTPException(status_code=400, detail="days_of_week 는 1(월)~7(일) 범위")


def _compute_next(kind: str, time_of_day: str, days: list[int] | None) -> str:
    """time_of_day 파싱 + next_run_at(UTC ISO) 계산. 파싱 실패는 400."""
    try:
        tod = parse_time_of_day(time_of_day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return compute_next_run(_now(), kind, tod, days).isoformat()


def _load_device_for_owner(sb: Any, device_uuid: str, user_id: str) -> None:
    res = (
        sb.table("devices").select("id, owner_id").eq("id", device_uuid).limit(1).execute()
    )
    row = (res.data or [None])[0]
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="device not found")


def _load_schedule_for_owner(sb: Any, schedule_id: str, user_id: str) -> dict[str, Any]:
    res = (
        sb.table("schedules").select("*").eq("id", schedule_id).limit(1).execute()
    )
    row = (res.data or [None])[0]
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="schedule not found")
    return row


# ---------- 엔드포인트 ----------

@device_schedules_router.post(
    "/{device_uuid}/schedules",
    response_model=ScheduleOut,
    status_code=status.HTTP_201_CREATED,
    summary="예약 생성",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_BAD},
)
def create_schedule(
    device_uuid: str,
    body: ScheduleCreate,
    user_id: str = Depends(get_current_user_id),
) -> ScheduleOut:
    """예약 등록. next_run_at(UTC) 을 서버가 계산해 저장한다 (KST 벽시계 → UTC)."""
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)
    _validate_action_payload(body.action, body.payload)
    _validate_days(body.kind, body.days_of_week)
    _validate_guard(body.guard, body.action)
    next_run = _compute_next(body.kind, body.time_of_day, body.days_of_week)

    row = {
        "device_id": device_uuid,
        "owner_id": user_id,
        "action": body.action,
        "payload": body.payload,
        "kind": body.kind,
        "time_of_day": body.time_of_day,
        "days_of_week": body.days_of_week if body.kind == "weekly" else None,
        "guard": body.guard,
        "enabled": body.enabled,
        "next_run_at": next_run,
    }
    res = sb.table("schedules").insert(row).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="schedule INSERT 실패")
    return ScheduleOut(**res.data[0])


@device_schedules_router.get(
    "/{device_uuid}/schedules",
    response_model=list[ScheduleOut],
    summary="디바이스 예약 목록",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def list_schedules(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> list[ScheduleOut]:
    sb = get_supabase_client()
    _load_device_for_owner(sb, device_uuid, user_id)
    res = (
        sb.table("schedules")
        .select("*")
        .eq("device_id", device_uuid)
        .eq("owner_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [ScheduleOut(**r) for r in (res.data or [])]


@schedules_router.patch(
    "/{schedule_id}",
    response_model=ScheduleOut,
    summary="예약 수정",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_BAD},
)
def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    user_id: str = Depends(get_current_user_id),
) -> ScheduleOut:
    """전송된 필드만 부분 수정. 타이밍(kind/time_of_day/days) 변경 시 next_run_at 재계산."""
    sb = get_supabase_client()
    existing = _load_schedule_for_owner(sb, schedule_id, user_id)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="변경 필드 없음")

    # 병합 후 값으로 검증/재계산 (부분 수정이라 기존값과 합쳐야 정확)
    merged_kind = updates.get("kind", existing["kind"])
    merged_tod = updates.get("time_of_day", existing["time_of_day"])
    merged_days = updates.get("days_of_week", existing.get("days_of_week"))

    if "action" in updates or "payload" in updates:
        # action 은 수정 불가 항목으로 두되, payload 변경 시 mist 재검증
        _validate_action_payload(existing["action"], updates.get("payload", existing.get("payload")))

    if "guard" in updates:
        # action 은 수정 불가라 기존값 기준으로 off+guard 조합 검사
        _validate_guard(updates["guard"], existing["action"])

    timing_changed = any(k in updates for k in ("kind", "time_of_day", "days_of_week"))
    if timing_changed:
        _validate_days(merged_kind, merged_days)
        updates["next_run_at"] = _compute_next(merged_kind, merged_tod, merged_days)
        # daily 로 바뀌면 days_of_week 정리
        if merged_kind == "daily":
            updates["days_of_week"] = None

    res = (
        sb.table("schedules")
        .update(updates)
        .eq("id", schedule_id)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="schedule not found")
    return ScheduleOut(**res.data[0])


@schedules_router.delete(
    "/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="예약 삭제",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def delete_schedule(
    schedule_id: str,
    user_id: str = Depends(get_current_user_id),
) -> None:
    sb = get_supabase_client()
    res = (
        sb.table("schedules")
        .delete()
        .eq("id", schedule_id)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="schedule not found")
