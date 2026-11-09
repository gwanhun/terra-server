"""
v4 온습도 판정 상태 기계 — Stage N (기획 v4 1-1~1-6).

계획: specs/stage-n-env-alerts-v4.md Task 1. DB·시계를 모르는 **순수** 모듈이다.
시각은 전부 인자로 받는다(`now_mono` = `time.monotonic()` 값, D7). 기록·푸시 적재는
`backend/alerts.py` 가 이 모듈이 낸 `Action` 을 보고 한다.

## 기준

| 요소 | 주의 (caution) | 경고 (danger) | 적정 |
|------|----------------|---------------|------|
| 온도 | 사용자 MIN 미만 / MAX 초과 (2분) | 15℃ 미만 / 29℃ 초과 (2분) | MIN ≤ 값 ≤ MAX |
| 습도 | 건조: MIN 미만 2분 / 과습: MAX 초과 **120분** | 없음 | MIN ≤ 값 ≤ MAX |

경계값은 적정(디폴트 MAX 27.0℃ 는 고온 주의 아님), 15.0·29.0℃ 는 경고 아님(주의).

## 전이 우선순위 (한 step 에 전이 1개)

| 활성 | 순서 |
|------|------|
| 없음 | 경고 진입(2분) > 주의 진입(2분, 과습 120분; D13) |
| 주의 | 경고 진입(2분) > 반대쪽 진입(D5) > 회복(적정 5분) |
| 경고 | 반대쪽 진입(D5) > 회복(적정 5분) > 완화(D3) |

전이가 없는 step 에서만 추가 푸시(D15)와 peak 갱신을 낸다.

## 결정 요약

- D1 유지 = 조건 **연속** 성립. 한 샘플이라도 깨지면 그 조건의 타이머는 처음부터.
- D2 샘플 간격 60초 초과 / 값 없음(None) / NaN → 이 요소의 타이머 전부 초기화.
  (60초 정확히는 유지. `a_ok=false` 는 호출측이 None 으로 넘긴다.)
- D3 경고 → 주의 완화: 같은 쪽 "경고 아님" 5분 + 그 시점 값이 주의 범위 →
  경고 close + 주의 open(push=False). 이 주의는 `silent` — 추가 푸시도 없다.
- D4 회복은 시간(5분)만 본다. 값 여유(히스테리시스) 없음.
- D5 진행 중 이벤트의 반대쪽 조건이 진입 시간만큼 유지 → close + 반대쪽 open(푸시).
- D13 경고 조건이 성립 중인 샘플에서는 주의 진입을 보류(주의 → 1분 뒤 경고 이중 푸시 방지).
- D14 무음 주의(D3) 시작 후 30분 안에 같은 쪽 경고가 시작되면 경고는 열되 push=False.
  추가 푸시는 체인 최초 경고의 푸시 시각 +1시간 기준으로 체인 전체에서 1회(이어받음).
- D15 추가 푸시: 전이 없음 + 푸시된 이벤트 + 아직 안 함 + 푸시 후 1시간 + **현재 값이
  이벤트 조건 만족**(회복 대기 중 "아직 위험" 오발송 방지). 이벤트당 1회.
- D16 open 은 push=True 로 내지만 `pushed_mono` 는 호출측이 적재 성공 후
  `mark_pushed()` 를 불러야 설정된다 → 적재 못 한 이벤트는 추가 푸시도 없다.

## 시각 규칙 (D11)

조건마다 "연속 성립 시작 시각"을 들고 있다가 `now - start >= 기준초` 면 유지로 본다.
open 의 `at_mono` = 그 조건 연속 성립 시작, close 의 `at_mono` = 적정(또는 완화·전환
조건) 연속 성립 시작. 전환(close + open)이면 두 Action 의 `at_mono` 가 같다.
peak 는 진입 대기 구간부터 추적하고, 진행 중 바뀌면 5분 뒤 `op="peak"` 로 알린다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal

# ---------- 상수 ----------

WARN_TEMP_LOW = 15.0
WARN_TEMP_HIGH = 29.0

DEFAULT_TEMP_MIN = 18.0
DEFAULT_TEMP_MAX = 27.0   # 2026-11-04 26 → 27 (앱 요청 — 기본 26 에서 고온 주의가 너무 쉽게 울림)
DEFAULT_HUMID_MIN = 40.0
DEFAULT_HUMID_MAX = 80.0

# 사용자 설정 허용 범위(v4 1-1·1-2). 저장값이 밖이면 clamp(D8).
TEMP_SET_RANGE = (15.0, 29.0)
HUMID_SET_RANGE = (35.0, 85.0)

ENTRY_SEC = 120               # 상태 진입 2분 유지
RECOVERY_SEC = 300            # 상태 회복(적정) 5분 유지 — D3 완화도 같은 5분
OVER_HUMID_ENTRY_SEC = 7200   # 과습 주의만 2시간 연속 (2026-11-09 1시간 → 2시간, 분무 뒤 건조 중 알림 방지)
REMINDER_SEC = 3600           # 최초 푸시 후 1시간 → 추가 푸시 1회
GAP_SEC = 60                  # 샘플 간격이 이보다 길면 공백(D2)
REDANGER_SILENCE_SEC = 1800   # D14 무음 재경고 창
PEAK_FLUSH_SEC = 300          # peak 바뀐 뒤 이만큼 지나면 op="peak"

Level = Literal["caution", "danger"]
Element = Literal["temp", "humid"]
Side = Literal["high", "low"]


# ---------- 설정 ----------

def _num(raw: dict[str, Any], key: str) -> float | None:
    """숫자로 쓸 수 있는 값만. None·문자열·bool·NaN 은 None(→ 디폴트)."""
    v = raw.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pair(lo: float | None, hi: float | None, rng: tuple[float, float],
          default: tuple[float, float]) -> tuple[float, float]:
    lo = default[0] if lo is None else min(max(lo, rng[0]), rng[1])
    hi = default[1] if hi is None else min(max(hi, rng[0]), rng[1])
    return (lo, hi) if lo < hi else default


@dataclass(frozen=True)
class Thresholds:
    """사용자 적정 범위(MIN/MAX). 경고 기준(15/29℃)은 고정이라 여기 없다."""

    temp_min: float
    temp_max: float
    humid_min: float
    humid_max: float

    @classmethod
    def default(cls) -> Thresholds:
        return cls(DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX, DEFAULT_HUMID_MIN, DEFAULT_HUMID_MAX)

    @classmethod
    def sanitize(cls, raw: dict[str, Any] | None) -> Thresholds:
        """device_settings 행 → Thresholds (D8).

        키: target_temp_min/max, target_humid_min/max. 없음·NULL → 디폴트,
        v4 범위 밖 → 범위로 clamp, clamp 후 MIN ≥ MAX → 그 요소만 디폴트.
        """
        raw = raw or {}
        t = _pair(_num(raw, "target_temp_min"), _num(raw, "target_temp_max"),
                  TEMP_SET_RANGE, (DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX))
        h = _pair(_num(raw, "target_humid_min"), _num(raw, "target_humid_max"),
                  HUMID_SET_RANGE, (DEFAULT_HUMID_MIN, DEFAULT_HUMID_MAX))
        return cls(t[0], t[1], h[0], h[1])


# ---------- 이벤트 / Action ----------

@dataclass
class ActiveEvent:
    """진행 중 이벤트. 트래커가 open 할 때 만들고, 호출측이 INSERT 후 `alert_id` 를 채운다.

    started_wall: 트래커는 wall clock 을 모르므로 open 직후엔 None — 호출측이 기록 시각
    (`wall(at_mono)`)으로 채운다. 재시작 복원 땐 DB `triggered_at`.
    silent: D3 완화로 열린 무음 주의. silent_since_mono 는 그 주의의 시작(at_mono) — D14 기준.
    chain_pushed_mono / chain_reminded: 무음 주의가 이어받은 "체인 최초 경고"의 푸시 시각·추가 푸시
    여부. D14 재경고는 이걸 자기 pushed_mono / reminded 로 받아 체인 기준 1시간 추가 푸시를 낸다.
    """

    kind: str
    level: Level
    started_wall: datetime | None
    pushed_mono: float | None
    reminded: bool
    silent: bool
    peak: float
    alert_id: str | None = None
    silent_since_mono: float | None = None
    chain_pushed_mono: float | None = None
    chain_reminded: bool = False


@dataclass(frozen=True)
class Action:
    """트래커가 호출측에 요청하는 일.

    - open: 새 이벤트 INSERT (+ push 면 started 푸시 적재)
    - close: `closes_alert_id` 행 종료(resolved_at = at_mono 의 wall)
    - remind: 추가 푸시(reminder) 적재
    - peak: 진행 중 이벤트의 context.peak_value 갱신
    threshold 는 경고라도 항상 넘은 쪽 **사용자** MIN/MAX(D10).
    """

    op: Literal["open", "close", "remind", "peak"]
    kind: str
    level: Level
    push: bool
    value: float | None
    threshold: float | None
    at_mono: float
    peak: float | None = None
    closes_alert_id: str | None = None


# ---------- 타이틀 (v4 1-5) ----------

_DANGER_TITLE = {
    ("env_temp_high", "danger"): "[고온 경고] 온도가 너무 높아 개체가 위험할 수 있어요",
    ("env_temp_low", "danger"): "[저온 경고] 온도가 너무 낮아 개체가 위험할 수 있어요",
}
_CAUTION_FMT = {
    "env_temp_high": "[고온 주의] 적정 온도보다 +{diff}℃",
    "env_temp_low": "[저온 주의] 적정 온도보다 -{diff}℃",
    "env_humid_low": "[건조 주의] 적정 습도보다 -{diff}%p",
    "env_humid_high": "[과습 안내] 적정 습도보다 +{diff}%p",
}


def display_diff(kind: str, value: float, threshold: float) -> float:
    """|값 − 기준| 을 표시 단위로 **올림**(D10). 온도 0.1℃, 습도 1%p, 최소 한 단위.

    float 그대로 올리면 26.3 − 26.0 = 0.3000000000000007 → 0.4 가 되므로
    repr 기준 Decimal 로 계산한다.
    """
    d = abs(Decimal(repr(float(value))) - Decimal(repr(float(threshold))))
    unit = Decimal("0.1") if kind.startswith("env_temp") else Decimal("1")
    d = max(d.quantize(unit, rounding=ROUND_CEILING), unit)
    return float(d)


def title(kind: str, level: Level, value: float, threshold: float | None) -> str:
    """푸시·alerts.message 타이틀. 경고는 고정 문구(diff 미표시), 주의는 diff 포함."""
    if level == "danger":
        try:
            return _DANGER_TITLE[(kind, level)]
        except KeyError:
            raise ValueError(f"경고 타이틀 없는 kind: {kind}") from None
    fmt = _CAUTION_FMT.get(kind)
    if fmt is None or threshold is None:
        raise ValueError(f"주의 타이틀에는 kind·threshold 가 필요: {kind}, {threshold}")
    diff = display_diff(kind, value, threshold)
    text = f"{diff:.1f}" if kind.startswith("env_temp") else f"{int(diff)}"
    return fmt.format(diff=text)


# ---------- 트래커 ----------

def _opp(side: Side) -> Side:
    return "low" if side == "high" else "high"


def _side_of(kind: str) -> Side:
    return "high" if kind.endswith("_high") else "low"


@dataclass
class _Run:
    """한 조건의 연속 성립 구간."""

    start: float
    ext: float                 # 구간 극값(high 쪽 조건은 최대, low 쪽은 최소)
    snap_event: ActiveEvent | None   # 구간 시작 때의 활성 이벤트
    snap_peak: float | None    # 그때 활성 이벤트의 peak (이 구간 값이 섞이기 전)


@dataclass
class ElementTracker:
    """요소(온도/습도) 하나의 v4 판정 상태. 기기 × 요소마다 하나.

    호출측 약속:
    - open(push=True) 을 적재 성공하면 `mark_pushed(now)`.
    - open INSERT 가 실패하면 `rollback_open()` (타이머는 남아 다음 샘플에서 다시 open).
    - INSERT 성공하면 `active.alert_id`·`active.started_wall` 을 채운다.
    - remind 를 내면 트래커가 `reminded=True` 로 둔다. 적재 실패로 다시 시도하려면
      호출측이 `active.reminded = False` 로 되돌린다.
    - 기준(Thresholds)이 바뀌면 `reset_timers()` (D8).
    """

    element: Element
    active: ActiveEvent | None = None
    _runs: dict[str, _Run] = field(default_factory=dict)
    _last_mono: float | None = None
    _peak_dirty_since: float | None = None

    # ----- 외부 조작 -----

    def restore(self, ev: ActiveEvent | None, now_mono: float) -> None:
        """재시작 복원. 타이머는 관측이 없으니 비우고 이벤트만 이어 간다(now_mono 는 기록용)."""
        del now_mono  # 시그니처 고정용 — 타이머를 비우므로 쓸 일이 없다
        self.active = ev
        self.reset_timers()
        self._peak_dirty_since = None

    def reset_timers(self) -> None:
        """진입·회복 타이머 전부 초기화. 활성 이벤트는 그대로."""
        self._runs.clear()
        self._last_mono = None

    def mark_pushed(self, now_mono: float) -> None:
        """started 푸시 적재 성공(D16). 이미 기록돼 있으면 덮어쓰지 않는다."""
        if self.active is not None and self.active.pushed_mono is None:
            self.active.pushed_mono = now_mono

    def rollback_open(self) -> None:
        """직전 open 취소(INSERT 실패). 활성만 해제하고 타이머는 유지."""
        self.active = None
        self._peak_dirty_since = None

    # ----- 판정 -----

    def _conds(self, v: float, th: Thresholds) -> dict[str, bool]:
        if self.element == "temp":
            lo, hi = th.temp_min, th.temp_max
            return {
                "danger_high": v > WARN_TEMP_HIGH,
                "danger_low": v < WARN_TEMP_LOW,
                "caution_high": v > hi,
                "caution_low": v < lo,
                "ok": lo <= v <= hi,
                "calm_high": v <= WARN_TEMP_HIGH,   # D3: 고온 경고 아님
                "calm_low": v >= WARN_TEMP_LOW,
            }
        lo, hi = th.humid_min, th.humid_max
        return {"caution_high": v > hi, "caution_low": v < lo, "ok": lo <= v <= hi}

    def _threshold(self, side: Side, th: Thresholds) -> float:
        if self.element == "temp":
            return th.temp_max if side == "high" else th.temp_min
        return th.humid_max if side == "high" else th.humid_min

    def _entry_sec(self, side: Side) -> int:
        if self.element == "humid" and side == "high":
            return OVER_HUMID_ENTRY_SEC
        return ENTRY_SEC

    def _held(self, name: str, now: float, sec: float) -> _Run | None:
        run = self._runs.get(name)
        return run if run is not None and now - run.start >= sec else None

    def _update_runs(self, now: float, v: float, conds: dict[str, bool]) -> None:
        for name, ok in conds.items():
            if not ok:
                self._runs.pop(name, None)
                continue
            run = self._runs.get(name)
            if run is None:
                a = self.active
                self._runs[name] = _Run(now, v, a, a.peak if a is not None else None)
            elif name.endswith("_low"):
                run.ext = min(run.ext, v)
            else:
                run.ext = max(run.ext, v)

    def _update_peak(self, now: float, v: float) -> None:
        a = self.active
        if a is None:
            return
        new = max(a.peak, v) if _side_of(a.kind) == "high" else min(a.peak, v)
        if new != a.peak:
            a.peak = new
            if self._peak_dirty_since is None:
                self._peak_dirty_since = now

    def _close(self, run: _Run, v: float, th: Thresholds) -> Action:
        """활성 이벤트를 run 시작 시각으로 닫는 Action. peak 는 run 값이 섞이기 전 것."""
        a = self.active
        assert a is not None
        peak = run.snap_peak if run.snap_event is a and run.snap_peak is not None else a.peak
        return Action(
            op="close", kind=a.kind, level=a.level, push=False, value=v,
            threshold=self._threshold(_side_of(a.kind), th), at_mono=run.start,
            peak=peak, closes_alert_id=a.alert_id,
        )

    def _open(self, side: Side, level: Level, run: _Run, v: float, th: Thresholds,
              *, push: bool = True, silent: bool = False,
              chain_from: ActiveEvent | None = None) -> Action:
        """chain_from: silent 이면 직전 경고(체인 정보 넘김), D14 재경고면 직전 무음 주의(받음)."""
        kind = f"env_{self.element}_{side}"
        self.active = ActiveEvent(
            kind=kind, level=level, started_wall=None, pushed_mono=None,
            reminded=False, silent=silent, peak=run.ext,
            silent_since_mono=run.start if silent else None,
        )
        if chain_from is not None:
            if silent:    # D3: 경고(체인 최초 경고 또는 D14 재경고)의 푸시 상태를 들고 간다
                self.active.chain_pushed_mono = chain_from.pushed_mono
                self.active.chain_reminded = chain_from.reminded
            else:         # D14: 체인 최초 푸시 기준으로 1시간 추가 푸시(이미 보냈으면 없음)
                self.active.pushed_mono = chain_from.chain_pushed_mono
                self.active.reminded = chain_from.chain_reminded
        self._peak_dirty_since = None
        return Action(
            op="open", kind=kind, level=level, push=push, value=v,
            threshold=self._threshold(side, th), at_mono=run.start, peak=run.ext,
        )

    def _in_danger(self, conds: dict[str, bool]) -> bool:
        return conds.get("danger_high", False) or conds.get("danger_low", False)

    def _transition(self, now: float, v: float, th: Thresholds,
                    conds: dict[str, bool]) -> list[Action]:
        a = self.active
        is_temp = self.element == "temp"

        # ① 활성 없음: 경고 진입 > 주의 진입(D13)
        if a is None:
            if is_temp:
                for side in ("high", "low"):
                    run = self._held(f"danger_{side}", now, ENTRY_SEC)
                    if run:
                        return [self._open(side, "danger", run, v, th)]
            if not self._in_danger(conds):
                for side in ("high", "low"):
                    run = self._held(f"caution_{side}", now, self._entry_sec(side))
                    if run:
                        return [self._open(side, "caution", run, v, th)]
            return []

        side: Side = _side_of(a.kind)
        opp = _opp(side)

        # ② 활성 주의: 경고 진입 > 반대쪽 진입(D5) > 회복
        if a.level == "caution":
            if is_temp:
                for s in (side, opp):
                    run = self._held(f"danger_{s}", now, ENTRY_SEC)
                    if run:
                        # D14: 무음 주의 시작 후 30분 안에 같은 쪽 경고 → 푸시 생략
                        quiet = (
                            s == side and a.silent and a.silent_since_mono is not None
                            and run.start - a.silent_since_mono <= REDANGER_SILENCE_SEC
                        )
                        close = self._close(run, v, th)
                        return [close, self._open(s, "danger", run, v, th, push=not quiet,
                                                  chain_from=a if quiet else None)]
            if not self._in_danger(conds):
                run = self._held(f"caution_{opp}", now, self._entry_sec(opp))
                if run:
                    close = self._close(run, v, th)
                    return [close, self._open(opp, "caution", run, v, th)]
            run = self._held("ok", now, RECOVERY_SEC)
            if run:
                close = self._close(run, v, th)
                self.active = None
                self._peak_dirty_since = None
                return [close]
            return []

        # ③ 활성 경고(온도만): 반대쪽 진입(D5) > 회복 > 완화(D3)
        run = self._held(f"danger_{opp}", now, ENTRY_SEC)
        if run:
            close = self._close(run, v, th)
            return [close, self._open(opp, "danger", run, v, th)]
        if not self._in_danger(conds):
            run = self._held(f"caution_{opp}", now, ENTRY_SEC)
            if run:
                close = self._close(run, v, th)
                return [close, self._open(opp, "caution", run, v, th)]
        run = self._held("ok", now, RECOVERY_SEC)
        if run:
            close = self._close(run, v, th)
            self.active = None
            self._peak_dirty_since = None
            return [close]
        run = self._held(f"calm_{side}", now, RECOVERY_SEC)
        if run and conds[f"caution_{side}"]:
            close = self._close(run, v, th)
            return [close, self._open(side, "caution", run, v, th, push=False, silent=True,
                                      chain_from=a)]
        return []

    def step(self, now_mono: float, value: float | None, th: Thresholds) -> list[Action]:
        """샘플 1건 판정. 전이가 있으면 그 Action 들만, 없으면 remind/peak 를 낸다."""
        if value is None or not math.isfinite(value):
            # D2: 값 없음·NaN → 이 요소 타이머 초기화. 판정·추가 푸시 없음.
            self.reset_timers()
            return []
        if self._last_mono is not None and now_mono - self._last_mono > GAP_SEC:
            self.reset_timers()   # D2: 공백
        self._last_mono = now_mono

        v = float(value)
        conds = self._conds(v, th)
        self._update_runs(now_mono, v, conds)
        self._update_peak(now_mono, v)

        acts = self._transition(now_mono, v, th, conds)
        if acts:
            return acts

        a = self.active
        if a is None:
            return []
        out: list[Action] = []
        side = _side_of(a.kind)
        thr = self._threshold(side, th)
        cond = f"{'danger' if a.level == 'danger' else 'caution'}_{side}"
        if (
            a.pushed_mono is not None
            and not a.reminded
            and now_mono - a.pushed_mono >= REMINDER_SEC
            and conds.get(cond, False)   # D15
        ):
            a.reminded = True
            out.append(Action(op="remind", kind=a.kind, level=a.level, push=True,
                              value=v, threshold=thr, at_mono=now_mono, peak=a.peak))
        if (
            self._peak_dirty_since is not None
            and now_mono - self._peak_dirty_since >= PEAK_FLUSH_SEC
        ):
            self._peak_dirty_since = None
            out.append(Action(op="peak", kind=a.kind, level=a.level, push=False,
                              value=v, threshold=thr, at_mono=now_mono, peak=a.peak))
        return out
