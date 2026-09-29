"""
액추에이터 상태 재조정 (2026-09-29, 설계 specs/camera-reliability-2026-09.md P2-(b)).

## 문제
예약은 이벤트 시각에 명령 1건만 넣고 결과를 보지 않는다. 명령이 유실되면(오프라인·무응답·거부)
다음 이벤트까지 어긋난 채다 — 09-29 냉각팬이 예약 OFF 뒤에도 1시간 넘게 돌았다. 재부팅 복원
(schedule_restore)은 부팅 직후 ON 만 다룬다.

## 동작
기기 telemetry 수신 시 기기당 CHECK_INTERVAL_SEC 에 한 번, 조명·팬·냉각팬별로
"telemetry 실제 상태" vs "예약상 지금 있어야 할 상태"(schedule_restore.latest_schedule_events —
재부팅 복원과 같은 계산)를 비교해 어긋나면 교정 명령 1회(source='reconcile').

안 건드리는 경우 (owner 결정 09-29: 수동 조작 존중, relay 제외):
- 이벤트 후 SETTLE_SEC 이내 — 예약 명령·재전달(5분)이 아직 진행 중일 수 있다.
- 그 이벤트 이후 같은 액추에이터에 수동·타이머 명령이 있었거나 가드가 스킵했다 → 그 구간 끝까지 존중.
- 최근 SETTLE_SEC 안에 어떤 명령이든 나갔다(restore 등 진행 중) → 이번엔 보류, 다음 확인 때 다시.
- 최근 명령 조회 실패 — 사용자 조작 여부를 모르면 교정하지 않는다(fail-safe).
- ON 교정인데 stop_when_* 가드 → 펌웨어가 조건 도달 시 스스로 끈 것일 수 있어 다시 켜지 않는다.
  skip_when_* 가드는 지금 조건으로 재평가(restore 와 같음).
- telemetry 에 상태 필드가 없음(구 펌웨어) — 실제 상태를 모른다.
- mist·relay·toggle·duration_ms one-shot — latest_schedule_events 가 애초에 제외.

한 예약 이벤트당 교정은 1회 — 교정이 또 실패해도 싸우지 않는다. 다음 이벤트가 오면 다시 1회 가능.
전제: 서버 명령이 액추에이터의 유일한 조작 경로(기기 물리 버튼 없음). 생기면 여기 규칙 재검토.
메모리 상태라 브리지 재시작 시 초기화 — 최악은 같은 이벤트에 교정 1회 더.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.command_service import DEFAULT_CMD_TTL_SEC, insert_pending_command
from backend.schedule_restore import ON_OFF_ACTIONS, latest_schedule_events

logger = logging.getLogger(__name__)

RECONCILE_SOURCE = "reconcile"
CHECK_INTERVAL_SEC = 300.0       # 기기당 확인 주기 (3초 telemetry 마다 schedules 조회 금지)
SETTLE_SEC = 360                 # 이벤트·최근 명령 후 대기 — 재전달 유예(5분)보다 길게
# 이 source 의 명령이 이벤트 이후 있으면 사용자(또는 가드)의 의도 → 그 구간은 교정 안 함
RESPECT_SOURCES = frozenset({"manual", "timer", "guard"})

_next_check: dict[str, float] = {}                 # device_uuid → 다음 확인 monotonic
_done: dict[tuple[str, str], str] = {}             # (device_uuid, actuator) → 교정 끝난 이벤트 시각
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _next_check.clear()
        _done.clear()


def _parse(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _recent_commands(sb: Any, device_uuid: str, actions: tuple[str, str],
                     since: datetime) -> list[dict[str, Any]]:
    res = (
        sb.table("commands")
        .select("source, issued_at, action")
        .eq("device_id", device_uuid)
        .in_("action", list(actions))
        .gte("issued_at", since.isoformat())
        .order("issued_at", desc=True)
        .limit(20)
        .execute()
    )
    return [r for r in (res.data or []) if isinstance(r, dict)]


def maybe_reconcile(
    sb: Any, device_uuid: str, label: str, state: dict[str, Any],
    now_utc: datetime | None = None, mono: float | None = None,
) -> list[dict[str, Any]]:
    """telemetry 마다 호출. 확인 주기가 됐고 어긋난 액추에이터가 있으면 교정 명령 큐잉. 큐잉한 행 반환."""
    t = time.monotonic() if mono is None else mono
    with _lock:
        if _next_check.get(device_uuid, float("-inf")) > t:
            return []
        _next_check[device_uuid] = t + CHECK_INTERVAL_SEC

    now = now_utc or datetime.now(timezone.utc)
    queued: list[dict[str, Any]] = []
    for act, (prev, row) in latest_schedule_events(sb, device_uuid, now).items():
        actual = state.get(act)
        if actual not in ("ON", "OFF"):
            continue
        on_action, off_action = ON_OFF_ACTIONS[act]
        expected = "ON" if row.get("action") == on_action else "OFF"
        if actual == expected:
            continue
        if (now - prev).total_seconds() < SETTLE_SEC:
            continue
        key = (device_uuid, act)
        event = prev.isoformat()
        with _lock:
            if _done.get(key) == event:
                continue

        guard = row.get("guard")
        if expected == "ON" and isinstance(guard, dict) and guard.get("enabled"):
            if str(guard.get("type", "")).startswith("stop_when_"):
                logger.info("reconcile %s: %s stop 가드 예약 — 펌웨어 판단 존중, 교정 안 함", label, act)
                with _lock:
                    _done[key] = event
                continue
            from backend.schedule_runner import _evaluate_skip_guard  # 지연 import (순환 회피)
            skip = _evaluate_skip_guard(sb, device_uuid, guard)
            if skip:
                logger.info("reconcile %s: %s 가드 스킵 — %s", label, act, skip["reason"])
                with _lock:
                    _done[key] = event
                continue

        try:
            recent = _recent_commands(sb, device_uuid, (on_action, off_action), prev)
        except Exception:  # noqa: BLE001 — 모르면 교정하지 않는다
            logger.warning("reconcile %s: %s 최근 명령 조회 실패 — 교정 보류", label, act, exc_info=True)
            continue
        if any(r.get("source") in RESPECT_SOURCES for r in recent):
            logger.info("reconcile %s: %s 이벤트(%s) 뒤 수동/가드 조작 있음 — 존중", label, act, event)
            with _lock:
                _done[key] = event
            continue
        settle_cut = now - timedelta(seconds=SETTLE_SEC)
        if any((ts := _parse(r.get("issued_at"))) is not None and ts > settle_cut for r in recent):
            continue  # 진행 중인 명령(restore·재전달 등) — 다음 확인 때 다시 본다

        action = on_action if expected == "ON" else off_action
        try:
            inserted = insert_pending_command(
                sb,
                device_uuid=device_uuid,
                action=action,
                payload=row.get("payload") if expected == "ON" else None,
                issued_by=row.get("owner_id"),
                ttl_sec=DEFAULT_CMD_TTL_SEC,
                source=RECONCILE_SOURCE,
                source_id=row.get("id"),
                reason=f"reconcile {act} {actual}→{expected} (schedule event {event})",
            )
        except Exception:  # noqa: BLE001 — 다음 확인 때 재시도
            logger.exception("reconcile %s: %s INSERT 실패", label, action)
            continue
        if inserted is None:
            continue
        with _lock:
            _done[key] = event
        logger.warning("reconcile %s: %s 실제 %s ≠ 예약 %s → %s 큐잉 (command %s)",
                       label, act, actual, expected, action, inserted.get("id"))
        queued.append(inserted)
    return queued


__all__ = [
    "CHECK_INTERVAL_SEC",
    "RECONCILE_SOURCE",
    "SETTLE_SEC",
    "maybe_reconcile",
    "reset",
]
