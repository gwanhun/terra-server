"""
v4 일일 온습도 분석 카드 — Stage P (기획 v4 2-1~2-3).

계획: specs/stage-p-daily-analysis-v4.md Task 1. DB·시계를 모르는 **순수** 모듈이다.
입력(alerts 의 v4 이벤트 행, telemetry_30m 버킷, Stage O 의 humid_cycles)은 호출측
(`backend/analysis_runner.py`)이 조회해 넘기고, 이 모듈은 카드 문구만 만든다.
문구 정본은 서버 하나 — 앱은 저장된 `Card.to_json()` 을 그대로 그린다.

## 구간

`report_date` 08:00 ~ 다음 날 08:00 (KST). 주간 08:00~20:00, 야간 20:00~08:00.

## 온도 카드 (실시간 이벤트 요약 > 주야간 온도차)

| 우선 | 조건 | code | status | 타이틀 |
|------|------|------|--------|--------|
| 1 | 고온 경고 | TEMP_DANGER_HIGH | danger | 위험한 온도가 기록되었어요 |
| 2 | 저온 경고 | TEMP_DANGER_LOW | danger | 위험한 온도가 기록되었어요 |
| 3 | 고온 주의 | TEMP_CAUTION_HIGH | caution | 높은 온도가 기록됐어요 |
| 4 | 저온 주의 | TEMP_CAUTION_LOW | caution | 낮은 온도가 기록됐어요 |
| - | 이벤트 없음, 주야간 차 < 1.5 | TEMP_DIFF_TOO_LOW | caution | 주간과 야간 온도가 거의 일정했어요 |
| - | 이벤트 없음, 1.5 ≤ 차 ≤ 6 | TEMP_NORMAL | normal | 주간과 야간 모두 적정 온도를 유지했어요 |
| - | 이벤트 없음, 차 > 6 | TEMP_DIFF_TOO_HIGH | caution | 주간과 야간의 온도차가 너무 커요 |

세부 줄은 구간과 겹치는 이벤트 전부, 시작 시각순.

## 습도 카드 (건조 > 과습 > 환기 지연 > 사이클 요약)

| 조건 | code | status | 타이틀 |
|------|------|--------|--------|
| 건조 주의 | HUM_DRY | caution | 건조를 주의해 주세요 |
| 과습 주의 | HUM_OVER | caution | 과습을 주의해 주세요 |
| 환기 지연 | HUM_DELAY | danger | 건습 사이클이 건강하지 않아요 |
| 이벤트 없음 + 사이클 ≥ 1·도달 ≥ 1 | HUM_NORMAL | normal | 건습 사이클이 건강하게 유지됐어요 |
| 이벤트 없음 + 사이클 0 | HUM_NO_MIST | caution | 습기가 감지되지 않았어요 |

`cycles` 가 None(Stage O 전)이면 이벤트 없는 날 습도 카드는 없다(P9).
status 는 환기 지연 줄이 하나라도 있으면 danger(기획 3-3 HUM_DELAY = 경고, 주간과 같은 어휘),
아니면 caution. 타이틀 우선순위(건조 > 과습 > 환기 지연)와는 따로 본다.

## 결정 요약

- P4 구간과 겹치는 이벤트(시작 < 구간 끝, 종료 없음 또는 종료 > 구간 시작)만. 진행 중이면 `{start}~`.
- P5 시각은 KST `HH:MM`. report_date 가 아닌 날 시각만 `M/D HH:MM`, 범위 끝은 시작과 같은 날이면 생략.
- P6 `{temp}` = peak_value(없으면 value). `{diff}` = peak_diff → |peak_value − threshold| →
  |value − threshold| → context.diff. 계산이 필요하면 Stage N D10(올림)과 같은 `display_diff` —
  종료 전·후 같은 이벤트가 같은 값으로 보이게. 값이 하나도 없으면 그 줄만 뺀다("None" 금지).
- P7 이벤트 카드는 집계와 무관하게 항상. 이벤트 없는 날 주야간 카드는 유효 수집일
  (`is_valid_day`, t_a) 이고 주간·야간 표본이 모두 있을 때만.
- P8 주야간 평균 = 버킷 `t_a_avg` 의 `t_a_count` 가중평균. 차이·판정은 **표시값** 기준:
  주간·야간 평균을 각각 소수 1자리 half-up 한 뒤 그 차이(앱이 보는 세 숫자가 서로 맞게).
- P11 같은 chain_root(없으면 자기 id)를 가진 행은 한 줄: 첫 시작 ~ 마지막 종료, 레벨 경고,
  `{temp}` = chain 안 가장 극단 peak. caution→danger 승격(chain_root 없음)은 별도 줄.
- P12 status: 경고(온도 경고·환기 지연) 있으면 danger, 주의만 caution, 주야간 1.5~6 normal / 그 외 caution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

from backend.env_rules import display_diff

# ---------- 상수 ----------

KST = timezone(timedelta(hours=9))

TEMP_EVENT_KINDS = ("env_temp_high", "env_temp_low")
HUMID_EVENT_KINDS = ("env_humid_low", "env_humid_high", "env_vent_delay")

DAY_START_HOUR = 8      # 주간 08:00~20:00, 그 외 야간. 일일 구간도 08:00 시작
NIGHT_START_HOUR = 20

EXPECTED_SAMPLES_PER_DAY = 28800   # 3초 주기 × 24시간
VALID_DAY_RATIO = 0.8              # 유효 수집일 = 지표 표본 합 ≥ 28800 × 0.8 (Stage Q Q1 공용)

DAYNIGHT_OK = (Decimal("1.5"), Decimal("6"))   # 표시값 기준, 양끝 포함

Element = Literal["temp", "humid"]
Status = Literal["danger", "caution", "normal"]
Level = Literal["caution", "danger"]
Window = tuple[datetime, datetime]

_UTC = timezone.utc


# ---------- 카드 ----------

@dataclass(frozen=True)
class Card:
    """일일 분석 카드 하나. `to_json()` 이 env_daily_reports.temp_card / humid_card 에 들어간다."""

    element: Element
    code: str | None
    status: Status
    title: str
    lines: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "code": self.code,
            "status": self.status,
            "title": self.title,
            "lines": list(self.lines),
            "data": dict(self.data),
        }


# ---------- 문구 (v4 2-2·2-3 원문) ----------

_TEMP_EVENT_CARD: list[tuple[str, Level, str, str]] = [   # 우선순위 순
    ("env_temp_high", "danger", "TEMP_DANGER_HIGH", "위험한 온도가 기록되었어요"),
    ("env_temp_low", "danger", "TEMP_DANGER_LOW", "위험한 온도가 기록되었어요"),
    ("env_temp_high", "caution", "TEMP_CAUTION_HIGH", "높은 온도가 기록됐어요"),
    ("env_temp_low", "caution", "TEMP_CAUTION_LOW", "낮은 온도가 기록됐어요"),
]
_HUMID_EVENT_CARD: list[tuple[str, str, str]] = [   # 우선순위 순
    ("env_humid_low", "HUM_DRY", "건조를 주의해 주세요"),
    ("env_humid_high", "HUM_OVER", "과습을 주의해 주세요"),
    ("env_vent_delay", "HUM_DELAY", "건습 사이클이 건강하지 않아요"),
]
_DAYNIGHT_ADVICE = "건강한 생체 리듬을 위해 주야 온도 차이 3~5℃를 권장해요"


# ---------- 숫자·시각 ----------

def _half_up(x: float, unit: str) -> Decimal:
    """표시 반올림(half-up). float 잡음(1.4499999999999993)을 1e-9 에서 먼저 정리한다."""
    d = Decimal(repr(float(x))).quantize(Decimal("1e-9"), rounding=ROUND_HALF_EVEN)
    return d.quantize(Decimal(unit), rounding=ROUND_HALF_UP)


def fmt_temp(x: float) -> str:
    """온도 표시: 소수 1자리 half-up. 25.15 → "25.2"."""
    return f"{_half_up(x, '0.1'):.1f}"


def fmt_humid(x: float) -> str:
    """습도 표시: 정수 half-up. 9.5 → "10"."""
    return f"{_half_up(x, '1'):.0f}"


def _num(v: Any) -> float | None:
    """숫자로 쓸 수 있는 값만. None·문자열·bool·NaN 은 None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_ts(v: Any) -> datetime | None:
    """timestamptz 문자열/datetime → aware datetime(UTC). naive 는 UTC 로 본다."""
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def report_window(report_date: date) -> tuple[datetime, datetime]:
    """report_date 08:00 KST ~ 다음 날 08:00 KST (P2). UTC aware 로 돌려준다."""
    start = datetime.combine(report_date, time(DAY_START_HOUR), tzinfo=KST)
    return start.astimezone(_UTC), (start + timedelta(days=1)).astimezone(_UTC)


def _fmt_time(t: datetime, window: Window, same_as: datetime | None = None) -> str:
    """P5 r2: report_date(구간 시작일) 시각이면 HH:MM, 다른 날이면 M/D HH:MM (KST).

    날짜 없는 시각은 앱 자정~자정 그래프의 그날로 읽힌다(앱 회신 §7). `same_as` 와
    같은 날이면 날짜를 생략한다 — 범위 끝 시각용(`11/3 02:00~03:00`).
    """
    k = t.astimezone(KST)
    hm = f"{k.hour:02d}:{k.minute:02d}"
    if k.date() == window[0].astimezone(KST).date():
        return hm
    if same_as is not None and k.date() == same_as.astimezone(KST).date():
        return hm
    return f"{k.month}/{k.day} {hm}"


def _fmt_range(start: datetime, end: datetime | None, window: Window) -> str:
    s = _fmt_time(start, window)
    return f"{s}~" if end is None else f"{s}~{_fmt_time(end, window, same_as=start)}"


def _in_window(t: datetime, window: Window) -> bool:
    return window[0] <= t < window[1]


# ---------- 이벤트 → 줄 ----------

@dataclass
class _Line:
    """카드 한 줄이 될 이벤트(같은 chain 의 행은 하나로 합쳐짐, P11)."""

    root: str
    kind: str
    level: Level
    start: datetime
    end: datetime | None
    rows: list[dict[str, Any]]


def _ctx(row: dict[str, Any]) -> dict[str, Any]:
    c = row.get("context")
    return c if isinstance(c, dict) else {}


def _row_level(row: dict[str, Any]) -> Level:
    lv = _ctx(row).get("level")
    if lv in ("caution", "danger"):
        return lv
    return "danger" if row.get("severity") == "critical" else "caution"


def _event_lines(events: list[dict[str, Any]], kinds: tuple[str, ...], window: Window) -> list[_Line]:
    """kind 로 거르고 chain_root 로 묶은 뒤 구간과 겹치는 것만(P4·P11), 시작 시각순.

    호출측은 겹치는 행이 가리키는 chain_root 행도 함께 넘긴다 — 그 행이 구간 밖이어도
    줄의 시작 시각을 정하는 데 쓰인다.
    """
    groups: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for i, row in enumerate(events):
        if row.get("kind") not in kinds:
            continue
        start = _parse_ts(row.get("triggered_at"))
        if start is None:
            continue
        own = str(row.get("id") or f"_row{i}")
        root = str(_ctx(row).get("chain_root") or own)
        groups.setdefault(root, []).append((start, row))

    lines: list[_Line] = []
    for root, items in groups.items():
        items.sort(key=lambda it: it[0])
        rows = [r for _, r in items]
        start = items[0][0]
        end = _parse_ts(rows[-1].get("resolved_at"))
        chained = len(rows) > 1 or any(_ctx(r).get("chain_root") for r in rows)
        level: Level = "danger" if chained else _row_level(rows[0])
        if start >= window[1] or (end is not None and end <= window[0]):
            continue   # P4: 구간과 겹치지 않음
        lines.append(_Line(root=root, kind=rows[0]["kind"], level=level, start=start, end=end, rows=rows))
    lines.sort(key=lambda ln: (_sort_time(ln), ln.kind))
    return lines


def _sort_time(ln: _Line) -> datetime:
    """줄 정렬 시각 = 줄에 표시되는 시작 시각(환기 지연은 분무 시작)."""
    if ln.kind == "env_vent_delay":
        return _parse_ts(_ctx(ln.rows[0]).get("cycle_started_at")) or ln.start
    return ln.start


def _is_high(kind: str) -> bool:
    return kind.endswith("_high")


def _peak(ln: _Line) -> float | None:
    """P6·P11: chain 안 가장 극단 peak(행마다 peak_value, 없으면 value)."""
    vals = []
    for r in ln.rows:
        c = _ctx(r)
        v = _num(c.get("peak_value"))
        if v is None:
            v = _num(c.get("value"))
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return max(vals) if _is_high(ln.kind) else min(vals)


def _row_diff(row: dict[str, Any]) -> float | None:
    """P6: peak_diff → |peak_value − threshold| → |value − threshold| → context.diff."""
    c = _ctx(row)
    d = _num(c.get("peak_diff"))
    if d is not None:
        return d
    thr = _num(c.get("threshold"))
    if thr is not None:
        for key in ("peak_value", "value"):
            v = _num(c.get(key))
            if v is not None:
                return display_diff(row["kind"], v, thr)
    return _num(c.get("diff"))


def _diff(ln: _Line) -> float | None:
    vals = [d for d in (_row_diff(r) for r in ln.rows) if d is not None]
    return max(vals) if vals else None


def _temp_line(ln: _Line, window: Window) -> str | None:
    rng = _fmt_range(ln.start, ln.end, window)
    if ln.level == "danger":
        temp = _peak(ln)
        return None if temp is None else f"{rng} 위험 온도 {fmt_temp(temp)}℃ 감지"
    diff = _diff(ln)
    if diff is None:
        return None
    sign = "+" if _is_high(ln.kind) else "-"
    return f"{rng} 적정 온도보다 최대 {sign}{fmt_temp(diff)}℃ 이탈"


def _humid_line(ln: _Line, window: Window) -> str | None:
    if ln.kind == "env_vent_delay":
        started = _parse_ts(_ctx(ln.rows[0]).get("cycle_started_at"))
        if started is None:
            return None   # Stage O 가 분무 시작을 안 남겼으면 6시간 늦은 시각을 쓰지 않고 생략
        return f"{_fmt_time(started, window)}에 분무 후 사육장이 충분히 마르지 않음"
    diff = _diff(ln)
    if diff is None:
        return None
    sign = "+" if _is_high(ln.kind) else "-"
    return f"{_fmt_range(ln.start, ln.end, window)} 적정 습도보다 최대 {sign}{fmt_humid(diff)}% 이탈"


def _events_data(lines: list[_Line]) -> list[dict[str, Any]]:
    return [
        {
            "root": ln.root,
            "ids": [str(r.get("id")) for r in ln.rows if r.get("id") is not None],
            "kind": ln.kind,
            "level": ln.level,
            "start": ln.start.isoformat(),
            "end": ln.end.isoformat() if ln.end is not None else None,
        }
        for ln in lines
    ]


# ---------- 주야간 ----------

def _count(v: Any) -> int:
    n = _num(v)
    return int(n) if n is not None and n > 0 else 0


def is_valid_day(buckets: list[dict[str, Any]], window: Window, metric: Literal["t_a", "h_a"]) -> bool:
    """유효 수집일: 구간 버킷의 `{metric}_count` 합 ≥ 28800 × 0.8 (Stage Q Q1 과 같은 정본)."""
    total = 0
    for b in buckets:
        ts = _parse_ts(b.get("bucket"))
        if ts is not None and _in_window(ts, window):
            total += _count(b.get(f"{metric}_count"))
    return total >= EXPECTED_SAMPLES_PER_DAY * VALID_DAY_RATIO


def _daynight_avgs(buckets: list[dict[str, Any]], window: Window) -> tuple[float | None, float | None]:
    """구간 버킷의 t_a_avg 를 t_a_count 로 가중평균(P8). 표본 0 인 쪽은 None."""
    acc = {"day": [0.0, 0], "night": [0.0, 0]}
    for b in buckets:
        ts = _parse_ts(b.get("bucket"))
        if ts is None or not _in_window(ts, window):
            continue
        avg, n = _num(b.get("t_a_avg")), _count(b.get("t_a_count"))
        if avg is None or n == 0:
            continue
        h = ts.astimezone(KST).hour
        side = acc["day" if DAY_START_HOUR <= h < NIGHT_START_HOUR else "night"]
        side[0] += avg * n
        side[1] += n
    day = acc["day"][0] / acc["day"][1] if acc["day"][1] else None
    night = acc["night"][0] / acc["night"][1] if acc["night"][1] else None
    return day, night


def _daynight_display(buckets: list[dict[str, Any]], window: Window) -> tuple[Decimal, Decimal, Decimal] | None:
    """(주간, 야간, 차이) 표시값. 주간·야간 중 하나라도 표본이 없으면 None."""
    day, night = _daynight_avgs(buckets, window)
    if day is None or night is None:
        return None
    d, n = _half_up(day, "0.1"), _half_up(night, "0.1")
    return d, n, abs(d - n)


def daynight_diff(buckets: list[dict[str, Any]], window: Window) -> float | None:
    """하루 주야간 온도차 |주간 − 야간| (표시값 기준, 소수 1자리). Stage Q 가 날짜별로 재사용."""
    disp = _daynight_display(buckets, window)
    return float(disp[2]) if disp is not None else None


def _daynight_data(buckets: list[dict[str, Any]], window: Window) -> dict[str, Any]:
    disp = _daynight_display(buckets, window)
    return {
        "day_avg": float(disp[0]) if disp else None,
        "night_avg": float(disp[1]) if disp else None,
        "diff_daynight": float(disp[2]) if disp else None,
        "valid_day": is_valid_day(buckets, window, "t_a"),
    }


# ---------- 카드 생성 ----------

def build_temp_card(events: list[dict[str, Any]], buckets: list[dict[str, Any]],
                    window: Window) -> Card | None:
    """온도 카드. 구간과 겹치는 온도 이벤트가 있으면 이벤트 요약, 없으면 주야간 온도차(P7)."""
    lines = _event_lines(events, TEMP_EVENT_KINDS, window)
    data = _daynight_data(buckets, window)
    if lines:
        present = {(ln.kind, ln.level) for ln in lines}
        code, title = next((c, t) for k, lv, c, t in _TEMP_EVENT_CARD if (k, lv) in present)
        status: Status = "danger" if any(ln.level == "danger" for ln in lines) else "caution"
        text = [s for s in (_temp_line(ln, window) for ln in lines) if s is not None]
        return Card("temp", code, status, title, text, {"events": _events_data(lines), **data})

    if not data["valid_day"] or data["diff_daynight"] is None:
        return None   # P7: 유효 수집일 아님 또는 주간·야간 한쪽 표본 없음
    diff = Decimal(str(data["diff_daynight"]))
    lo, hi = DAYNIGHT_OK
    if diff < lo:
        code, status, title = "TEMP_DIFF_TOO_LOW", "caution", "주간과 야간 온도가 거의 일정했어요"
        body = _DAYNIGHT_ADVICE
    elif diff <= hi:
        code, status, title = "TEMP_NORMAL", "normal", "주간과 야간 모두 적정 온도를 유지했어요"
        body = "안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요"
    else:
        code, status, title = "TEMP_DIFF_TOO_HIGH", "caution", "주간과 야간의 온도차가 너무 커요"
        body = f"주간·야간 평균 편차 {diff:.1f}℃. {_DAYNIGHT_ADVICE}"
    return Card("temp", code, status, title, [body], {"events": [], **data})


def build_humid_card(events: list[dict[str, Any]], buckets: list[dict[str, Any]],
                     window: Window, cycles: list[dict[str, Any]] | None = None) -> Card | None:
    """습도 카드. 습도 이벤트 요약 > (cycles 가 있을 때만) 건습 사이클 요약(P9).

    buckets 는 온도 카드와 시그니처를 맞추려고 받는다(현재 습도 카드 판정에는 안 쓴다).
    """
    del buckets
    lines = _event_lines(events, HUMID_EVENT_KINDS, window)
    if lines:
        present = {ln.kind for ln in lines}
        code, title = next((c, t) for k, c, t in _HUMID_EVENT_CARD if k in present)
        status: Status = "danger" if "env_vent_delay" in present else "caution"
        text = [s for s in (_humid_line(ln, window) for ln in lines) if s is not None]
        return Card("humid", code, status, title, text, {"events": _events_data(lines)})

    if cycles is None:
        return None   # P9: Stage O 전에는 이벤트 없는 날 습도 카드 없음
    in_win = []
    for c in cycles:
        started = _parse_ts(c.get("started_at"))
        if started is None or _in_window(started, window):
            in_win.append(c)
    wet = len(in_win)
    reached = sum(
        1 for c in in_win
        if _parse_ts(c.get("dry_reached_at")) is not None or c.get("status") in ("reached_in_time", "reached_late")
    )
    data = {"events": [], "wet_count": wet, "dry_reach_count": reached}
    if wet == 0:
        return Card("humid", "HUM_NO_MIST", "caution", "습기가 감지되지 않았어요",
                    ["분무를 잊지 않으셨는지 확인해 주세요"], data)
    if reached >= 1:
        return Card("humid", "HUM_NORMAL", "normal", "건습 사이클이 건강하게 유지됐어요",
                    [f"습기 {wet}번 , 건기 {reached}번이 기록됐어요"], data)
    return None   # 사이클은 있는데 도달 0 — 기획 문구 없음(보통 환기 지연 이벤트가 먼저 잡힌다)
