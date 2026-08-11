"""
예약 스케줄 계산 — Stage H-2.

`time_of_day` (KST) + kind(daily/weekly) 로 **다음 실행 시각(UTC)** 을 계산한다.

## 왜 KST 기준?
사용자가 앱에서 "매일 08:00" 이라고 하면 그건 KST 08:00 이다. DB 에는 UTC 로
저장/비교하므로, KST 벽시계 시각을 UTC 로 환산해야 한다. clip 날짜 폴더 버그와
동일한 함정 — 반드시 Asia/Seoul ZoneInfo 로 환산.

## compute_next_run 규칙
- **항상 now 보다 미래**의 가장 가까운 실행 시각을 반환.
- 이 성질 덕에 브리지가 한동안 죽어 next_run_at 이 과거가 돼도, 발화 시 한 번만
  실행하고 다음 미래 시각으로 점프한다 (밀린 예약 폭주 없음).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

VALID_KINDS: tuple[str, ...] = ("daily", "weekly")


def parse_time_of_day(value: str) -> time:
    """'HH:MM' 또는 'HH:MM:SS' (DB TIME 반환형) → datetime.time (KST 벽시계)."""
    parts = value.split(":")
    if len(parts) < 2:
        raise ValueError(f"time_of_day 형식 오류: {value!r} (HH:MM 필요)")
    hh, mm = int(parts[0]), int(parts[1])
    ss = int(parts[2]) if len(parts) >= 3 else 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError(f"time_of_day 범위 오류: {value!r}")
    return time(hour=hh, minute=mm, second=ss)


def compute_next_run(
    now_utc: datetime,
    kind: str,
    time_of_day: time,
    days_of_week: list[int] | None = None,
) -> datetime:
    """now_utc 이후 가장 가까운 실행 시각을 UTC 로 반환.

    days_of_week: weekly 전용. ISO 요일 (1=월 .. 7=일).
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"지원하지 않는 kind: {kind!r}")
    if kind == "weekly" and not days_of_week:
        raise ValueError("weekly 는 days_of_week 가 최소 1개 필요")

    now_kst = now_utc.astimezone(KST)

    # 오늘(0)부터 7일 뒤까지 탐색 — 어떤 요일이든 7일 내 반드시 매칭.
    for offset in range(0, 8):
        cand_date = (now_kst + timedelta(days=offset)).date()
        cand_kst = datetime.combine(cand_date, time_of_day, tzinfo=KST)
        if cand_kst <= now_kst:
            continue  # 이미 지난 시각 (오늘분 포함) 스킵
        if kind == "daily":
            return cand_kst.astimezone(timezone.utc)
        # weekly
        if cand_kst.isoweekday() in days_of_week:  # type: ignore[operator]
            return cand_kst.astimezone(timezone.utc)

    # 도달 불가 (daily 는 offset 1 이내, weekly 는 7 이내 매칭 보장)
    raise RuntimeError("next_run 계산 실패 — 로직 점검 필요")


__all__ = [
    "KST",
    "VALID_KINDS",
    "compute_next_run",
    "parse_time_of_day",
]
