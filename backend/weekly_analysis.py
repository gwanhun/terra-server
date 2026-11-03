"""
v4 주간 온습도 분석 카드·예외 — Stage Q (기획 v4 3-1~3-3, 4-1).

계획: specs/stage-q-weekly-analysis-v4.md Task 1. DB·시계를 모르는 **순수** 모듈이다.
입력(alerts 의 v4 이벤트 행, 이번 주·직전 주 telemetry_30m 버킷, Stage O 의 humid_cycles,
devices.created_at)은 호출측(`backend/analysis_runner.py`)이 조회해 넘긴다.
일일 리포트에는 의존하지 않는다 — 일일 생성이 빠진 날이 있어도 주간은 원본에서 다시 센다.

## 구간

`week_start`(월요일) 08:00 ~ 다음 월요일 08:00 (KST). 일별 판정(유효 수집일·주야간 차)은
Stage P 의 하루 구간(08:00~08:00) 7개로 나눠 본다.

## 온도 카드 (대표 케이스 1개, 위에서부터)

| 케이스 | 조건(표시값 기준) | status | 세부 |
|--------|------------------|--------|------|
| TEMP_DANGER | 경고 chain ≥ 1 | danger | [1] 횟수·최대 이탈, [3] 반복 시간대(Q7) |
| TEMP_CAUTION | 주의 chain ≥ 4 | caution | [1] 횟수 |
| TEMP_DIFF_HIGH | 두 주 모두 유효 7일, |주 평균 차| ≥ 3.0 | caution | [2] 상승/하락 |
| TEMP_DIFF_TOO_LOW | 유효일 주야간 차 평균 < 1.5 | caution | [3] |
| TEMP_DIFF_TOO_HIGH | 평균 > 6.0 | caution | [3] |
| TEMP_NORMAL | 1.5 ≤ 평균 ≤ 6.0 | normal | [3] |

주야간 평균을 낼 유효일이 없으면 온도 카드 없음(Q9).

## 습도 카드

HUM_DELAY(환기 지연 ≥ 2, danger) → HUM_DRY / HUM_OVER / HUM_MIXED → HUM_UNSTABLE →
HUM_NORMAL(사이클 ≥ 1·도달 ≥ 1, normal) → HUM_NO_MIST(습도 이벤트 0·사이클 0, caution, ㉕).
나머지는 caution. `cycles` 가 None(Stage O 전·켜기 전 주)이면 HUM_DELAY·HUM_NORMAL·HUM_NO_MIST 는
판정하지 않는다(Q10). 사이클 소속은 05:00 경계(`cycle_window`), 도달은 주 구간 끝까지만 센다.

## 예외 (지표별, `notices`)

- ERR_NO_DATA: 유효 수집일 < 3 → 그 지표 카드 없이 안내만(Q3).
- ERR_DISCONNECT: 유효 수집일 ≥ 3 이고 누락률 ≥ 50% → 그 지표의 주의·경고 이벤트가 있으면
  안내 없이 카드만, 없으면 안내만(Q4 r2, 기획자 답 2026-11-03). 이벤트가 카드 조건에 못 미쳐
  카드가 없으면 안내.

## 결정 요약

- Q1 유효 수집일 = Stage P `is_valid_day`(지표 표본 합 ≥ 28800 × 0.8) — 기준 정본은 P 상수.
- Q2 누락률 = 1 − 구간 표본 합 / 예정 표본. 예정 = (구간 끝 − max(구간 시작, 등록 시각)) × (28800/86400).
- Q5 횟수 = 구간과 겹치는 chain 수. chain(경고 → 무음 주의 → 무음 재경고)은 1건, 경고로 분류(P11).
- Q6 최대 이탈 = 경고 chain 들의 사용자 기준 대비 이탈 최댓값(P6 대체 규칙).
- Q7 패턴: 경고 chain 이 **시작한** (날짜, 시)를 세어 같은 시가 서로 다른 3일 이상이면 그 시간대
  (동률이면 이른 시 = 0시 쪽). 없으면 경고가 시작된 날 ≥ 3 이고 전부 주간/야간이면 `주간 시간`/`야간 시간`.
  날짜는 KST 달력 날짜. 지난주에 시작해 넘어온 chain 은 시간대에 넣지 않는다(횟수에는 들어감).
- Q8 주 평균 = 버킷 t_a_avg 의 t_a_count 가중평균, 표시값(소수 1자리 half-up)끼리의 차.
- Q11 `{dry_time}` = 구간 안에서 시작해 건기에 도달한 사이클의 평균 소요 분 → `N시간 M분`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from backend.daily_analysis import (
    DAY_START_HOUR,
    EXPECTED_SAMPLES_PER_DAY,
    HUMID_EVENT_KINDS,
    KST,
    NIGHT_START_HOUR,
    TEMP_EVENT_KINDS,
    Card,
    EventChain,
    Status,
    Window,
    chain_diff,
    cycle_window,
    daynight_diff,
    event_chains,
    events_data,
    fmt_temp,
    half_up,
    is_valid_day,
    num,
    parse_ts,
    reached_by,
    report_window,
    sample_count,
)

Metric = Literal["t_a", "h_a"]

# ---------- 상수 ----------

WEEK_DAYS = 7
MIN_VALID_DAYS = 3                 # Q3: 미만이면 ERR_NO_DATA
DISCONNECT_RATE = 0.5              # Q4: 누락률 ≥ 50% 면 ERR_DISCONNECT
SAMPLES_PER_SEC = EXPECTED_SAMPLES_PER_DAY / 86400   # 3초 주기 (Q2)

TEMP_CAUTION_MIN = 4               # 주의 chain ≥ 4 → TEMP_CAUTION
TEMP_DIFF_PREV_MIN = Decimal("3.0")
DAYNIGHT_LOW = Decimal("1.5")      # 미만 → TOO_LOW
DAYNIGHT_HIGH = Decimal("6.0")     # 초과 → TOO_HIGH
PATTERN_MIN_DAYS = 3               # Q7

HUM_DELAY_MIN = 2
HUM_ONE_MIN = 3                    # 건조/과습 ≥ 3
HUM_OTHER_MAX = 2                  # 다른 쪽 ≤ 2

_REACHED = ("reached_in_time", "reached_late")

# ---------- 문구 (v4 3-2·3-3·4-1 원문, 이중 공백은 하나로) ----------

_NOTICE = {
    "ERR_NO_DATA": ("주간 분석을 위한 데이터를 모으고 있어요", "최소 3일의 데이터가 필요해요"),
    "ERR_DISCONNECT": ("주간 데이터 수집이 원활하지 않았어요", "센서 전원과 Wi-Fi 연결 상태를 확인해 주세요"),
}


# ---------- 결과 ----------

@dataclass(frozen=True)
class WeeklyResult:
    """한 기기·한 주의 주간 분석. 카드가 None 이면 그 지표 카드 없음(Q3·Q9·Q10)."""

    temp_card: Card | None
    humid_card: Card | None
    notices: list[dict[str, str]] = field(default_factory=list)


# ---------- 구간·집계 ----------

def week_window(week_start: date) -> tuple[datetime, datetime]:
    """week_start 08:00 KST ~ +7일 08:00 KST. UTC aware 로 돌려준다."""
    start = datetime.combine(week_start, time(DAY_START_HOUR), tzinfo=KST)
    end = start + timedelta(days=WEEK_DAYS)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _days(window: Window) -> list[date]:
    """구간을 이루는 하루(08~08) 7개의 시작 날짜(KST)."""
    first = window[0].astimezone(KST).date()
    return [first + timedelta(days=i) for i in range(WEEK_DAYS)]


def valid_days(buckets: list[dict[str, Any]], window: Window, metric: Metric) -> list[date]:
    """Q1: 구간 7일 중 유효 수집일(Stage P `is_valid_day`)의 날짜 목록."""
    return [d for d in _days(window) if is_valid_day(buckets, report_window(d), metric)]


def _in_window(ts: datetime, window: Window) -> bool:
    return window[0] <= ts < window[1]


def _samples(buckets: list[dict[str, Any]], window: Window, metric: Metric) -> int:
    total = 0
    for b in buckets:
        ts = parse_ts(b.get("bucket"))
        if ts is not None and _in_window(ts, window):
            total += sample_count(b.get(f"{metric}_count"))
    return total


def missing_rate(buckets: list[dict[str, Any]], window: Window, metric: Metric,
                 registered_at: datetime | str | None) -> float | None:
    """Q2: 1 − 구간 표본 합 / 예정 표본. 예정은 등록 이후만 센다. 예정 0 이하면 None.

    등록 시각을 모르면 구간 시작부터 센다. 표본이 예정보다 많으면(버킷 경계 등록 등) 0.0.
    """
    reg = parse_ts(registered_at)
    begin = max(window[0], reg) if reg is not None else window[0]
    expected = (window[1] - begin).total_seconds() * SAMPLES_PER_SEC
    if expected <= 0:
        return None
    return max(0.0, 1.0 - _samples(buckets, window, metric) / expected)


def _week_avg(buckets: list[dict[str, Any]], window: Window) -> float | None:
    """Q8: 구간 버킷 t_a_avg 의 t_a_count 가중평균. 표본 0 이면 None."""
    s, n = 0.0, 0
    for b in buckets:
        ts = parse_ts(b.get("bucket"))
        if ts is None or not _in_window(ts, window):
            continue
        avg, c = num(b.get("t_a_avg")), sample_count(b.get("t_a_count"))
        if avg is None or c == 0:
            continue
        s += avg * c
        n += c
    return s / n if n else None


# ---------- 온도 ----------

def _start_slot(chain: EventChain, window: Window) -> tuple[date, int] | None:
    """Q7: chain 이 **시작한** (KST 날짜, 시). 구간 밖(지난주)에서 시작했으면 None.

    걸친 시간대를 전부 세면 하루 가까이 이어진 경고가 거의 모든 시간대를 3일 이상으로 만들어
    동률 최소인 "0시~1시"가 뽑혔다(리플레이 2026-11-02) — "언제 문제가 생기나"를 보려고 시작만 센다.
    """
    if not window[0] <= chain.start < window[1]:
        return None
    t = chain.start.astimezone(KST)
    return t.date(), t.hour


def _time_label(chains: list[EventChain], window: Window) -> str | None:
    """Q7: `{H}시~{H+1}시 사이` / `주간 시간` / `야간 시간` / None(줄 생략). 경고 시작 시각 기준."""
    slots = {s for s in (_start_slot(ch, window) for ch in chains) if s is not None}
    days_by_hour: dict[int, set[date]] = {}
    for d, h in slots:
        days_by_hour.setdefault(h, set()).add(d)
    best = max((len(ds) for ds in days_by_hour.values()), default=0)
    if best >= PATTERN_MIN_DAYS:
        h = min(h for h, ds in days_by_hour.items() if len(ds) == best)
        return f"{h}시~{(h + 1) % 24}시 사이"
    if len({d for d, _ in slots}) < PATTERN_MIN_DAYS:
        return None
    hours = {h for _, h in slots}
    if all(DAY_START_HOUR <= h < NIGHT_START_HOUR for h in hours):
        return "주간 시간"
    if all(not DAY_START_HOUR <= h < NIGHT_START_HOUR for h in hours):
        return "야간 시간"
    return None


def _daynight_week(buckets: list[dict[str, Any]], days: list[date]) -> Decimal | None:
    """Q9: 유효 수집일들의 일별 주야간 차(표시값) 평균을 다시 표시값으로. 없으면 None."""
    diffs = [x for x in (daynight_diff(buckets, report_window(d)) for d in days) if x is not None]
    if not diffs:
        return None
    mean = sum(Decimal(str(x)) for x in diffs) / len(diffs)
    return half_up(float(mean), "0.1")


def _temp_card(events: list[dict[str, Any]], buckets: list[dict[str, Any]],
               prev_buckets: list[dict[str, Any]] | None, window: Window,
               t_days: list[date], stats: dict[str, Any]) -> Card | None:
    chains = event_chains(events, TEMP_EVENT_KINDS, window)
    dangers = [c for c in chains if c.level == "danger"]
    cautions = [c for c in chains if c.level == "caution"]
    diffs = [d for d in (chain_diff(c) for c in dangers) if d is not None]
    max_dev = max(diffs) if diffs else None

    # 전주 대비(Q8): 두 주 모두 유효 7일일 때만
    this_avg = _week_avg(buckets, window)
    prev_avg: float | None = None
    diff_prev: Decimal | None = None
    signed: Decimal | None = None
    if prev_buckets is not None and len(t_days) == WEEK_DAYS:
        prev_w = (window[0] - timedelta(days=WEEK_DAYS), window[1] - timedelta(days=WEEK_DAYS))
        if len(valid_days(prev_buckets, prev_w, "t_a")) == WEEK_DAYS:
            prev_avg = _week_avg(prev_buckets, prev_w)
            if this_avg is not None and prev_avg is not None:
                signed = half_up(this_avg, "0.1") - half_up(prev_avg, "0.1")
                diff_prev = abs(signed)

    diff_dn = _daynight_week(buckets, t_days)
    data: dict[str, Any] = {
        **stats,
        "n_danger": len(dangers),
        "n_temp_caution": len(cautions),
        "max_deviation": max_dev,
        "week_avg": float(half_up(this_avg, "0.1")) if this_avg is not None else None,
        "prev_week_avg": float(half_up(prev_avg, "0.1")) if prev_avg is not None else None,
        "diff_prev": float(diff_prev) if diff_prev is not None else None,
        "diff_daynight": float(diff_dn) if diff_dn is not None else None,
        "events": events_data(chains),
    }

    if dangers:
        lines = []
        if max_dev is not None:
            lines.append(f"총 {len(dangers)}회 위험 온도 발생. 적정 온도보다 최대 {fmt_temp(max_dev)}℃ 이탈")
        else:   # 이탈 폭을 못 구해도 횟수는 남긴다 — 타이틀만 있는 빈 카드 방지
            lines.append(f"총 {len(dangers)}회 위험 온도 발생.")
        label = _time_label(dangers, window)
        if label is not None:
            lines.append(f"{label}에 온도 이탈이 집중되었어요")
        return Card("temp", "TEMP_DANGER", "danger", "주간 중 위험 온도가 감지되었어요", lines, data)
    if len(cautions) >= TEMP_CAUTION_MIN:
        return Card("temp", "TEMP_CAUTION", "caution", "적정 온도를 벗어난 경우가 많았어요",
                    [f"총 {len(cautions)}회 적정 온도 범위를 벗어났어요"], data)
    if diff_prev is not None and signed is not None and diff_prev >= TEMP_DIFF_PREV_MIN:
        up = signed > 0
        title = "지난주보다 평균 온도가 높아졌어요" if up else "지난주보다 평균 온도가 낮아졌어요"
        line = f"지난주 대비 평균 온도가 {diff_prev:.1f}℃ {'상승' if up else '하락'}했어요"
        return Card("temp", "TEMP_DIFF_HIGH", "caution", title, [line], data)
    if diff_dn is None:
        return None   # Q9: 주야간 판정 불가 → TEMP_NORMAL 도 안 냄
    if diff_dn < DAYNIGHT_LOW:
        return Card("temp", "TEMP_DIFF_TOO_LOW", "caution", "주·야간 온도차가 거의 없었어요",
                    [f"주야간 평균 온도차가 {diff_dn:.1f}℃로 작아요. 3~5℃ 차이를 권장해요"], data)
    if diff_dn > DAYNIGHT_HIGH:
        return Card("temp", "TEMP_DIFF_TOO_HIGH", "caution", "주·야간 온도차가 컸어요",
                    [f"주야간 평균 온도차가 {diff_dn:.1f}℃로 커요. 3~5℃ 내외를 권장해요"], data)
    return Card("temp", "TEMP_NORMAL", "normal", "주·야간 온도차가 안정적으로 유지되었어요",
                [f"주야간 평균 온도차가 {diff_dn:.1f}℃로 안정적이에요"], data)


# ---------- 습도 ----------

def fmt_dry_time(minutes: float) -> str:
    """Q11: 분 → `N시간 M분`(half-up 분). M=0 이면 `N시간`, N=0 이면 `M분`."""
    total = int(half_up(minutes, "1"))
    h, m = divmod(total, 60)
    if h == 0:
        return f"{m}분"
    return f"{h}시간" if m == 0 else f"{h}시간 {m}분"


def _cycle_stats(cycles: list[dict[str, Any]], window: Window) -> tuple[int, list[float]]:
    """사이클 구간(05:00 경계, O-f) 안에서 시작한 사이클 수, 그중 주 구간 끝까지 건기 도달한
    사이클의 소요 분 목록(Q11·Q12)."""
    n = 0
    mins: list[float] = []
    cw = cycle_window(window)
    for c in cycles:
        started = parse_ts(c.get("started_at"))
        if started is None or not _in_window(started, cw):
            continue
        n += 1
        reached = parse_ts(c.get("dry_reached_at"))
        if (reached is not None and reached >= started and reached_by(c, window[1])
                and c.get("status", _REACHED[0]) in _REACHED):
            mins.append((reached - started).total_seconds() / 60)
    return n, mins


def _humid_card(events: list[dict[str, Any]], window: Window,
                cycles: list[dict[str, Any]] | None, stats: dict[str, Any]) -> Card | None:
    chains = event_chains(events, HUMID_EVENT_KINDS, window)
    n_dry = sum(1 for c in chains if c.kind == "env_humid_low")
    n_over = sum(1 for c in chains if c.kind == "env_humid_high")
    n_delay = sum(1 for c in chains if c.kind == "env_vent_delay")
    data: dict[str, Any] = {**stats, "n_dry": n_dry, "n_over": n_over, "n_delay": n_delay,
                            "events": events_data(chains)}
    dry_time: str | None = None
    if cycles is not None:
        n_cycles, mins = _cycle_stats(cycles, window)
        if mins:
            dry_time = fmt_dry_time(sum(mins) / len(mins))
        data.update({"wet_count": n_cycles, "dry_reach_count": len(mins), "dry_time": dry_time})

    def card(code: str, status: Status, title: str, line: str | None) -> Card:
        return Card("humid", code, status, title, [line] if line is not None else [], data)

    if cycles is not None and n_delay >= HUM_DELAY_MIN:
        line = (f"총 {n_delay}회 환기 지연이 발생. 건기 도달까지 평균 {dry_time}이 걸렸어요"
                if dry_time is not None
                else f"총 {n_delay}회 환기 지연이 발생했어요")   # 건기 도달 사이클이 없어도 횟수는 남긴다 — 빈 카드 방지
        return card("HUM_DELAY", "danger", "사육장 환기가 지연되는 경향이 보여요", line)
    if n_dry >= HUM_ONE_MIN and n_over >= HUM_ONE_MIN:
        return card("HUM_MIXED", "caution", "건조와 과습이 반복됐어요",
                    f"건조 주의 {n_dry}회, 과습 주의 {n_over}회 발생. 분무량과 환기 상태를 함께 점검해 주세요")
    if n_dry >= HUM_ONE_MIN and n_over <= HUM_OTHER_MAX:
        return card("HUM_DRY", "caution", "사육장이 건조한 날이 많았어요",
                    f"총 {n_dry}회 건조 주의가 발생. 사육장에 수분을 보충해 주세요")
    if n_over >= HUM_ONE_MIN and n_dry <= HUM_OTHER_MAX:
        return card("HUM_OVER", "caution", "사육장이 습한날이 많았어요",
                    f"총 {n_over}회 과습 주의가 발생. 환기 상태를 점검해 주세요")
    if n_dry + n_over >= HUM_ONE_MIN:   # 여기 오면 둘 다 ≤ 2
        return card("HUM_UNSTABLE", "caution", "습도 이탈이 반복됐어요",
                    f"총 {n_dry + n_over}회 습도 이탈 발생. 일정한 루틴으로 분무를 관리해 주세요")
    if cycles is not None and data["wet_count"] >= 1 and dry_time is not None:
        return card("HUM_NORMAL", "normal", "건습 사이클이 건강하게 유지되었어요",
                    f"건기 도달 평균 시간 {dry_time}으로 건습 사이클을 유지했어요")
    if cycles is not None and not chains and data["wet_count"] == 0:
        # ㉕: 3-3 "(대신 예외 멘트가 출력됨)" — 일일 2-3 같은 상황 문구를 그대로(Stage O)
        return card("HUM_NO_MIST", "caution", "습기가 감지되지 않았어요", "분무를 잊지 않으셨는지 확인해 주세요")
    return None


# ---------- 공용 ----------

def _notice(code: str, metric: Metric) -> dict[str, str]:
    title, body = _NOTICE[code]
    return {"code": code, "metric": metric, "title": title, "body": body}


# ---------- 진입점 ----------

def build_weekly(events: list[dict[str, Any]], buckets: list[dict[str, Any]],
                 prev_buckets: list[dict[str, Any]] | None, window: Window,
                 registered_at: datetime | str | None,
                 cycles: list[dict[str, Any]] | None = None) -> WeeklyResult:
    """한 기기·한 주의 온도·습도 카드와 예외 안내(Q3·Q4).

    prev_buckets 는 직전 주 버킷(None = 첫 주, 전주 대비 생략). cycles 는 Stage O 이후에만.
    """
    notices: list[dict[str, str]] = []
    cards: dict[Metric, Card | None] = {"t_a": None, "h_a": None}
    for metric in ("t_a", "h_a"):
        days = valid_days(buckets, window, metric)
        if len(days) < MIN_VALID_DAYS:
            notices.append(_notice("ERR_NO_DATA", metric))
            continue
        rate = missing_rate(buckets, window, metric, registered_at)
        # Q4 r2: 끊긴 주는 주의·경고 이벤트가 있으면 카드만, 없으면 안내만
        disconnected = rate is not None and rate >= DISCONNECT_RATE
        kinds = TEMP_EVENT_KINDS if metric == "t_a" else HUMID_EVENT_KINDS
        if disconnected and not event_chains(events, kinds, window):
            notices.append(_notice("ERR_DISCONNECT", metric))
            continue
        stats = {"valid_days": len(days), "missing_rate": rate}
        if metric == "t_a":
            cards[metric] = _temp_card(events, buckets, prev_buckets, window, days, stats)
        else:
            cards[metric] = _humid_card(events, window, cycles, stats)
        if disconnected and cards[metric] is None:   # 이벤트가 카드 조건에 못 미치면 빈칸 대신 안내
            notices.append(_notice("ERR_DISCONNECT", metric))
    return WeeklyResult(cards["t_a"], cards["h_a"], notices)
