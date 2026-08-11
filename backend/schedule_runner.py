"""
예약 러너 — Stage H-2.

매 주기(기본 30초)로 `schedules` 에서 due 된 예약(next_run_at <= now, enabled)을 찾아:
1. next_run_at 을 다음 미래 시각으로 **먼저** 갱신 (+ last_run_at)
2. `commands` 에 pending 명령 INSERT

발행은 기존 CommandDispatcher 가 담당 — 러너는 명령을 "예약대로 큐잉"만 한다.

## 왜 advance 를 먼저?
mist 는 중복 발화(물 두 번)가 스킵보다 나쁘다. next_run_at 을 먼저 미래로 밀면,
INSERT 전에 프로세스가 죽어도 다음 주기에 재발화하지 않는다 (한 번 놓침 < 중복).
단일 인스턴스 가정이라 advisory lock 불필요 (dispatcher/offline_monitor 와 동일 전제).

## 밀린 예약 폭주 방지
compute_next_run 이 항상 now 이후를 반환 → 브리지가 오래 죽어도 발화는 1회.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from backend.command_service import DEFAULT_CMD_TTL_SEC, insert_pending_command
from backend.scheduling import compute_next_run, parse_time_of_day
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 30.0
DEFAULT_BATCH = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fire_one(sb: Any, row: dict[str, Any], now: datetime) -> None:
    """예약 1건 발화: next_run_at 갱신(먼저) → commands INSERT."""
    tod = parse_time_of_day(row["time_of_day"])
    next_run = compute_next_run(now, row["kind"], tod, row.get("days_of_week"))

    # 1) advance 먼저 (중복 발화 방지)
    sb.table("schedules").update({
        "next_run_at": next_run.isoformat(),
        "last_run_at": now.isoformat(),
    }).eq("id", row["id"]).execute()

    # 2) 명령 큐잉 — issued_by 는 owner_id (앱 이력에서 본인 것으로 표시)
    inserted = insert_pending_command(
        sb,
        device_uuid=row["device_id"],
        action=row["action"],
        payload=row.get("payload"),
        issued_by=row.get("owner_id"),
        ttl_sec=DEFAULT_CMD_TTL_SEC,
    )
    if inserted is None:
        logger.error("schedule %s 발화: 명령 INSERT 실패", row.get("id"))
    else:
        logger.info(
            "schedule %s 발화 → command %s (%s) next=%s",
            row.get("id"), inserted.get("id"), row["action"], next_run.isoformat(),
        )


def run_once(batch: int = DEFAULT_BATCH) -> int:
    """1회 스캔 — due 예약 발화. 발화한 건수 반환."""
    sb = get_supabase_client()
    now = _now()

    res = (
        sb.table("schedules")
        .select("id, device_id, owner_id, action, payload, kind, time_of_day, days_of_week")
        .eq("enabled", True)
        .lte("next_run_at", now.isoformat())
        .order("next_run_at")
        .limit(batch)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return 0

    fired = 0
    for row in rows:
        try:
            _fire_one(sb, row, now)
            fired += 1
        except Exception:  # noqa: BLE001
            logger.exception("schedule 발화 실패 (id=%s)", row.get("id"))
    return fired


class ScheduleRunner:
    """별도 스레드에서 주기 스캔. OfflineMonitor 와 동일 라이프사이클."""

    def __init__(self, interval_sec: float = DEFAULT_INTERVAL_SEC) -> None:
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="schedule-runner"
        )
        self._thread.start()
        logger.info("schedule runner 시작 (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("schedule runner 정지")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                n = run_once()
                if n:
                    logger.info("schedule runner: %d 건 발화", n)
            except Exception:  # noqa: BLE001
                logger.exception("schedule runner scan 실패")
            self._stop.wait(self._interval)


__all__ = [
    "DEFAULT_BATCH",
    "DEFAULT_INTERVAL_SEC",
    "ScheduleRunner",
    "run_once",
]
