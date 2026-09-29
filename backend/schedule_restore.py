"""
재부팅 후 예약 상태 복원 (2026-09-28, 앱 회신 §2).

## 문제
schedule_runner 는 next_run_at 이 된 순간에만 명령을 큐잉한다. 조명 예약 08:00 ON / 20:00 OFF 인
기기가 14:00 에 재부팅하면(원격 reboot · 브라운아웃 · 크래시 모두) 액추에이터는 전부 OFF 로 부팅되고,
다음 ON 은 내일 08:00 이라 그때까지 꺼진 채다.

## 동작
기기 telemetry 의 uptime_sec 으로 재부팅을 감지하면(maybe_restore), 그 기기의 enabled 예약 중
**켜짐/꺼짐 상태가 있는 것**(조명·팬·냉각팬)만 골라 액추에이터별로 "지금 이전 가장 최근 이벤트"를
구하고, 그것이 ON 이면 ON 명령을 1회 큐잉한다.

- 제외: mist/spray(1회성), relay_*(펌프 — 연속 가동 복원은 침수 위험), *_toggle(절대 상태 아님),
  payload.duration_ms 가 있는 one-shot 예약(팬 타이머).
- 가드: 예약의 skip_when_* 를 복원에도 적용한다. 원래 시각에 스킵됐을 조건이면 지금도 켜지 않는다.
- source='restore' — 예약 푸시(source='schedule' 만)가 나가지 않게 별도 값. commands_source_check
  제약에 'restore' 추가 필요(migrations/2026-09-28_commands_source_restore.sql).
- 부팅 1회당 1번만: boot 시각(now - uptime)을 기억해 같은 부팅의 후속 telemetry 는 무시한다.
  브리지 재시작 직후엔 직전 값이 없으므로 uptime 이 REBOOT_FRESH_UPTIME_SEC 미만인 첫 telemetry 만
  재부팅으로 본다(오래 켜져 있던 기기는 복원하지 않음).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from backend.command_service import DEFAULT_CMD_TTL_SEC, insert_pending_command
from backend.scheduling import compute_prev_run, parse_time_of_day

logger = logging.getLogger(__name__)

RESTORE_SOURCE = "restore"
RESTORE_REASON = "reboot_restore"

# 액추에이터 → (on, off). 여기 없는 action 은 복원 대상이 아니다.
ON_OFF_ACTIONS: dict[str, tuple[str, str]] = {
    "led":  ("led_on", "led_off"),
    "fan":  ("fan_on", "fan_off"),
    "fan2": ("fan2_on", "fan2_off"),
}
RESTORABLE_ACTIONS: frozenset[str] = frozenset(a for pair in ON_OFF_ACTIONS.values() for a in pair)

REBOOT_FRESH_UPTIME_SEC = 60      # 직전 값 없을 때(브리지 재시작) 이보다 짧은 uptime 만 재부팅으로
BOOT_TOLERANCE_SEC = 30           # boot 시각 추정 오차(telemetry 지연·시계 드리프트) 허용

_last_boot: dict[str, float] = {}   # device_uuid → 마지막으로 본 boot epoch
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _last_boot.clear()


def note_uptime(device_uuid: str, uptime_s: int | float, now: float | None = None) -> bool:
    """uptime 을 기록하고, 새 부팅으로 판단되면 True (호출자가 복원 실행)."""
    now = time.time() if now is None else now
    boot_ts = now - float(uptime_s)
    with _lock:
        last = _last_boot.get(device_uuid)
        if last is None:
            _last_boot[device_uuid] = boot_ts
            return uptime_s < REBOOT_FRESH_UPTIME_SEC
        if boot_ts > last + BOOT_TOLERANCE_SEC:
            _last_boot[device_uuid] = boot_ts
            return True
        return False


def _actuator_of(action: str) -> str | None:
    for act, pair in ON_OFF_ACTIONS.items():
        if action in pair:
            return act
    return None


def latest_schedule_events(
    sb: Any, device_uuid: str, now: datetime
) -> dict[str, tuple[datetime, dict[str, Any]]]:
    """액추에이터(led/fan/fan2)별로 now 이전 가장 최근 예약 이벤트 (시각, schedules 행).

    enabled 예약 중 on/off 절대 상태만 본다 — duration_ms one-shot(팬 타이머)은 상태가 아니라 제외.
    restore(재부팅 복원)와 actuator_reconcile(상태 재조정)이 같은 "지금 있어야 할 상태" 계산을 쓴다.
    """
    res = (
        sb.table("schedules")
        .select("id, device_id, owner_id, action, payload, kind, time_of_day, days_of_week, guard")
        .eq("device_id", device_uuid)
        .eq("enabled", True)
        .in_("action", sorted(RESTORABLE_ACTIONS))
        .execute()
    )
    rows = [r for r in (res.data or []) if isinstance(r, dict)]

    # 액추에이터별로 now 이전 가장 최근 이벤트
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for row in rows:
        act = _actuator_of(str(row.get("action", "")))
        if act is None:
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("duration_ms"):
            continue  # one-shot(팬 타이머) — 상태가 아니라 복원하지 않음
        try:
            prev = compute_prev_run(
                now, row["kind"], parse_time_of_day(row["time_of_day"]), row.get("days_of_week"))
        except (ValueError, RuntimeError, KeyError):
            logger.warning("restore: 예약 %s 시각 계산 실패 — 건너뜀", row.get("id"), exc_info=True)
            continue
        cur = latest.get(act)
        if cur is None or prev > cur[0]:
            latest[act] = (prev, row)
    return latest


def restore_after_reboot(
    sb: Any, device_uuid: str, label: str, now_utc: datetime | None = None
) -> list[dict[str, Any]]:
    """예약 기준으로 지금 켜져 있어야 할 액추에이터의 ON 명령을 큐잉. 큐잉한 command 행 목록 반환."""
    now = now_utc or datetime.now(timezone.utc)
    latest = latest_schedule_events(sb, device_uuid, now)

    queued: list[dict[str, Any]] = []
    for act, (prev, row) in latest.items():
        on_action = ON_OFF_ACTIONS[act][0]
        if row["action"] != on_action:
            continue  # 마지막 이벤트가 OFF → 꺼진 채가 맞다
        guard = row.get("guard")
        if isinstance(guard, dict) and guard.get("enabled"):
            from backend.schedule_runner import _evaluate_skip_guard  # 지연 import (순환 회피)
            skip = _evaluate_skip_guard(sb, device_uuid, guard)
            if skip:
                logger.info("restore %s: %s 가드 스킵 — %s", label, on_action, skip["reason"])
                continue
        inserted = insert_pending_command(
            sb,
            device_uuid=device_uuid,
            action=on_action,
            payload=row.get("payload"),
            issued_by=row.get("owner_id"),
            ttl_sec=DEFAULT_CMD_TTL_SEC,
            source=RESTORE_SOURCE,
            source_id=row.get("id"),
            reason=RESTORE_REASON,
        )
        if inserted is None:
            logger.error("restore %s: %s 명령 INSERT 실패", label, on_action)
            continue
        logger.info("restore %s: %s 복원 큐잉 → command %s (예약 %s, 마지막 이벤트 %s)",
                    label, on_action, inserted.get("id"), row.get("id"), prev.isoformat())
        queued.append(inserted)
    return queued


def maybe_restore(sb: Any, device_uuid: str, label: str, uptime_s: int | float) -> list[dict[str, Any]]:
    """telemetry 마다 호출. 새 부팅이면 복원 실행, 아니면 아무것도 안 함."""
    if not note_uptime(device_uuid, uptime_s):
        return []
    logger.info("restore %s: 재부팅 감지(uptime=%ss) → 예약 상태 복원", label, uptime_s)
    return restore_after_reboot(sb, device_uuid, label)


__all__ = [
    "BOOT_TOLERANCE_SEC",
    "ON_OFF_ACTIONS",
    "REBOOT_FRESH_UPTIME_SEC",
    "RESTORABLE_ACTIONS",
    "RESTORE_REASON",
    "RESTORE_SOURCE",
    "latest_schedule_events",
    "maybe_restore",
    "note_uptime",
    "reset",
    "restore_after_reboot",
]
