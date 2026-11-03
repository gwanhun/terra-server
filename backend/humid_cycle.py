"""
v4 습기 사이클 판정 — Stage O (기획 v4 0-2·1-2·1-3·1-6). **순수** 상태 기계: DB·시계를 모른다.

계획: specs/stage-o-humid-cycle-v4.md Task 1. 호출측(`backend/humid_cycle_store.py`)이 기기별로
`HumidCycleTracker` 를 들고, 습도 샘플마다 `step(샘플 시각, 습도)` 를 불러 나온 `CycleOp` 을 DB 에 쓴다.

## 규칙

| 항목 | 값 |
|------|----|
| 습도 | 유효 샘플의 30초 이동 중앙값(튀는 샘플 하나는 묻힘) |
| 습기 이벤트(O-a) | 지난 3분 최저값 대비 +10%p(최저값 < 60%) 또는 +5%p(최저값 ≥ 60%), 그리고 현재 ≥ 60%. 시작 = 그 최저값 시각 |
| 같은 상승 | 최저값 시각이 현재 사이클 MAX 창 끝(`max_until`)보다 이르면 같은 상승 — 무시 |
| 병합(O-g) | 시작 후 20분 안·미도달 사이클에 새 상승 → 같은 이벤트, MAX 창 재개·건기 목표 재확정 |
| MAX(O-c·O-d) | 감지 시각 + 3분까지 최고값, 상한 85%. 건기 목표 = MAX − 12%p |
| 건기 도달(O2) | 목표 확정 뒤 목표 이하 첫 샘플. 유효 경과 ≤ 6시간이면 권장 시간 내 |
| 환기 지연(O3·O-h) | 미도달로 유효 경과 6시간. 15시간까지 안 마르면 닫음(stale) |
| 분무 공백(O-e) | 최근 사이클이 건기에 닿았고 시작부터 유효 15시간 동안 새 습기 이벤트 없음 |
| 공백 | 유효 샘플 간격 > 60초 → 간격 전체를 최근 사이클 `offline_sec` 에 더하고 창을 비움. 공백 뒤 표본 5개·이력 60초 전엔 감지 안 함 |

"유효 경과" = 사이클 시작부터 벽시계 경과 − `offline_sec`.

한 샘플 안 순서: 공백 → 감지(open/merge) → MAX → 건기 도달 → 환기 지연 → 환기 지연 stale → 분무 공백.
새 사이클을 열 때 op 순서는 `open` → `not_reached`(직전 미도달) → `vent_delay_close` → `mist_gap_close`
(호출측이 open 을 먼저 써야 직전 종료를 쓴다).
"""

from __future__ import annotations

import copy
import logging
import os
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

logger = logging.getLogger(__name__)

# ---------- 상수 (계획 Global Constraints) ----------

SMOOTH_SEC = 30
SMOOTH_MIN_SAMPLES = 5
RISE_WINDOW_SEC = 180
RISE_MIN_HISTORY_SEC = 60
RISE_PP = 10.0
RISE_PP_HIGH_START = 5.0
HIGH_START_PCT = 60.0
WET_MIN_PCT = 60.0
MAX_WINDOW_SEC = 180
MAX_CAP_PCT = 85.0
DRY_DROP_PP = 12.0
MERGE_SEC = 1200
VENT_DELAY_SEC = 21600
VENT_STALE_SEC = 54000
MIST_GAP_SEC = 54000
GAP_SEC = 60

_TRUE = ("1", "true", "yes", "on")
_UTC = timezone.utc

Status = Literal["open", "reached_in_time", "reached_late", "not_reached"]
OpName = Literal["open", "merge", "max_fixed", "dry_reached", "not_reached", "vent_delay",
                 "vent_delay_close", "mist_gap", "mist_gap_close", "offline"]
REACHED: tuple[str, ...] = ("reached_in_time", "reached_late")


# ---------- 스위치 ----------

def humid_cycles_enabled() -> bool:
    """`HUMID_CYCLES_ENABLED` — 브리지 판정·분석 러너 공용."""
    return (os.getenv("HUMID_CYCLES_ENABLED") or "").strip().lower() in _TRUE


def humid_cycles_since() -> datetime | None:
    """`HUMID_CYCLES_SINCE` — 판정을 켠 시각. 없거나 못 읽으면 None(판정·카드 안 함)."""
    raw = (os.getenv("HUMID_CYCLES_SINCE") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("HUMID_CYCLES_SINCE 형식 오류(ISO8601 필요): %r", raw)
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=_UTC)).astimezone(_UTC)


# ---------- 상태 ----------

@dataclass
class CycleState:
    """습기 이벤트 1개(= humid_cycles 1행)."""

    cycle_id: str | None
    started_at: datetime
    start_humid: float
    detected_at: datetime          # 마지막 (병합) 상승 감지 시각
    peak: float                    # 지금까지 관측한 최고값(상한 전) = max_raw
    max_until: datetime            # MAX 측정 창 끝 = detected_at + 3분
    dry_target: float | None       # None = MAX 확정 전
    dry_reached_at: datetime | None
    status: Status
    offline_sec: float
    merged_count: int
    vent_delay_alert_id: str | None
    vent_delay_fired: bool
    vent_delay_closed: bool
    mist_gap_alert_id: str | None
    mist_gap_fired: bool
    mist_gap_closed: bool


@dataclass(frozen=True)
class CycleOp:
    op: OpName
    at: datetime
    cycle: CycleState
    value: float | None
    reason: str | None = None      # vent_delay_close: dry / next_event / stale


def _r1(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def max_humid(peak: float) -> float:
    """MAX 표시값: 상한 85%, 소수 1자리 반올림(O-d)."""
    return _r1(min(peak, MAX_CAP_PCT))


def effective_elapsed(c: CycleState, now: datetime) -> float:
    """사이클 시작부터 벽시계 경과 − 센서 공백(초)."""
    return (now - c.started_at).total_seconds() - c.offline_sec


# ---------- 상태 기계 ----------

class HumidCycleTracker:
    """기기 하나의 습기 사이클 판정. `cycle` 은 가장 최근 사이클(열림·닫힘 무관, 없으면 None)."""

    def __init__(self, cycle: CycleState | None = None, last_sample_at: datetime | None = None) -> None:
        self.cycle = cycle
        self._last_at = last_sample_at
        self._raw: deque[tuple[datetime, float]] = deque()     # 30초 중앙값용 원값
        self._mins: deque[tuple[datetime, float]] = deque()    # 3분 최저값(단조 증가)
        self._hist_start: datetime | None = None

    # 호출측이 DB 쓰기 실패 때 그 step 이전으로 되돌린다
    def snapshot(self) -> object:
        return copy.deepcopy((self.cycle, self._last_at, self._raw, self._mins, self._hist_start))

    def restore_snapshot(self, s: object) -> None:
        self.cycle, self._last_at, self._raw, self._mins, self._hist_start = copy.deepcopy(s)  # type: ignore[misc]

    def step(self, now: datetime, humid: float | None) -> list[CycleOp]:
        if humid is None:
            return []
        if self._last_at is not None and now <= self._last_at:
            return []   # 역순·중복 샘플
        ops: list[CycleOp] = []
        if self._last_at is not None:
            gap = (now - self._last_at).total_seconds()
            if gap > GAP_SEC:
                self._raw.clear()
                self._mins.clear()
                self._hist_start = None
                if self.cycle is not None:
                    self.cycle.offline_sec += gap
                    ops.append(CycleOp("offline", now, self.cycle, humid))
        self._last_at = now
        if self._hist_start is None:
            self._hist_start = now

        s = self._smooth(now, humid)
        ops += self._detect(now, s)
        c = self.cycle
        if c is None:
            return ops
        if c.status == "open" and c.dry_target is None and now > c.max_until:
            c.dry_target = _r1(max_humid(c.peak) - DRY_DROP_PP)
            ops.append(CycleOp("max_fixed", now, c, s))
        elif c.status == "open" and c.dry_target is None:
            c.peak = max(c.peak, s)
        eff = effective_elapsed(c, now)
        if c.status == "open" and c.dry_target is not None and s <= c.dry_target:
            c.dry_reached_at = now
            c.status = "reached_in_time" if eff <= VENT_DELAY_SEC else "reached_late"
            ops.append(CycleOp("dry_reached", now, c, s))
            if c.vent_delay_fired and not c.vent_delay_closed:
                c.vent_delay_closed = True
                ops.append(CycleOp("vent_delay_close", now, c, s, "dry"))
        if c.status == "open" and not c.vent_delay_fired and eff >= VENT_DELAY_SEC:
            c.vent_delay_fired = True
            ops.append(CycleOp("vent_delay", now, c, s))
        if (c.status == "open" and c.vent_delay_fired and not c.vent_delay_closed
                and eff >= VENT_STALE_SEC):
            c.vent_delay_closed = True
            ops.append(CycleOp("vent_delay_close", now, c, s, "stale"))
        if c.status in REACHED and not c.mist_gap_fired and eff >= MIST_GAP_SEC:
            c.mist_gap_fired = True
            ops.append(CycleOp("mist_gap", now, c, s))
        return ops

    def _smooth(self, now: datetime, humid: float) -> float:
        self._raw.append((now, humid))
        while self._raw[0][0] < now - timedelta(seconds=SMOOTH_SEC):
            self._raw.popleft()
        s = float(statistics.median(v for _, v in self._raw))
        while self._mins and self._mins[-1][1] >= s:   # 같은 값이면 뒤 샘플을 남긴다
            self._mins.pop()
        self._mins.append((now, s))
        while self._mins[0][0] < now - timedelta(seconds=RISE_WINDOW_SEC):
            self._mins.popleft()
        return s

    def _detect(self, now: datetime, s: float) -> list[CycleOp]:
        assert self._hist_start is not None
        if (len(self._raw) < SMOOTH_MIN_SAMPLES
                or (now - self._hist_start).total_seconds() < RISE_MIN_HISTORY_SEC):
            return []
        t0, low = self._mins[0]
        need = RISE_PP_HIGH_START if low >= HIGH_START_PCT else RISE_PP
        if s - low < need or s < WET_MIN_PCT:
            return []
        prev = self.cycle
        if prev is not None and t0 < prev.max_until:
            return []   # 앞 상승의 연장
        if (prev is not None and prev.status == "open"
                and (now - prev.started_at).total_seconds() <= MERGE_SEC):
            prev.detected_at = now
            prev.max_until = now + timedelta(seconds=MAX_WINDOW_SEC)
            prev.dry_target = None
            prev.merged_count += 1
            prev.peak = max(prev.peak, s)
            return [CycleOp("merge", now, prev, s)]

        new = CycleState(
            cycle_id=None, started_at=t0, start_humid=_r1(low), detected_at=now, peak=s,
            max_until=now + timedelta(seconds=MAX_WINDOW_SEC), dry_target=None, dry_reached_at=None,
            status="open", offline_sec=0.0, merged_count=0,
            vent_delay_alert_id=None, vent_delay_fired=False, vent_delay_closed=False,
            mist_gap_alert_id=None, mist_gap_fired=False, mist_gap_closed=False,
        )
        ops = [CycleOp("open", t0, new, s)]
        if prev is not None:
            if prev.status == "open":
                prev.status = "not_reached"
                ops.append(CycleOp("not_reached", t0, prev, s))
            if prev.vent_delay_fired and not prev.vent_delay_closed:
                prev.vent_delay_closed = True
                ops.append(CycleOp("vent_delay_close", t0, prev, s, "next_event"))
            if prev.mist_gap_fired and not prev.mist_gap_closed:
                prev.mist_gap_closed = True
                ops.append(CycleOp("mist_gap_close", t0, prev, s))
        self.cycle = new
        return ops


__all__ = [
    "MAX_CAP_PCT",
    "REACHED",
    "CycleOp",
    "CycleState",
    "HumidCycleTracker",
    "effective_elapsed",
    "humid_cycles_enabled",
    "humid_cycles_since",
    "max_humid",
]
