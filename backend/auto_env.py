"""
자동 환기·자동 분무 판정 상태 기계 — Stage R (앱 요청 2026-11-05 §3).

계획: specs/stage-r-auto-env.md Task 1. DB·시계를 모르는 **순수** 모듈이다. 경과·연속 판정은
전부 인자로 받은 단조 시간(`mono`, `alerts._now_mono()` 값)으로 하고, wall 시각은 KST 날짜와
`state_json` 의 표시 시각에만 쓴다. 명령 INSERT·섀도 로그·복원 쿼리는 `backend/auto_env_store.py` 가
이 모듈이 낸 `AutoCmd` 를 보고 한다.

## 판정 요약 (규칙 번호는 계획 R1~R13)

| 항목 | 규칙 |
|------|------|
| 입력(R1) | 온도·습도 값 없음 또는 샘플 간격 > 60초 → 연속 타이머 초기화, 그 샘플은 명령 없음 |
| 원인(R2) | `temp_high` = 온도 > MAX 연속 180초. `humid_high` = 습도 > MAX **이고** 마지막 습기 신호 뒤 60분 **이고** 온도 > MIN, 이 셋이 연속 180초 |
| 시작(R4) | 토글 켜짐·`fan` 키 있음·원인 ≥ 1·쉼 아님·예약 켜짐 아님·자동 종료 뒤 600초·오늘 누적 < 12시간·`fan == 'OFF'` → `fan_on {duration_ms: 3600000}` |
| 소유(R4) | 자동 `fan_on` 의 ACK ok 때만 확정. 실패면 소유 없음 + 직전 명령 뒤 300초에 재판정 |
| 종료(R5) | 서 있던 원인이 모두 종료선 이하 연속 60초 → `auto_vent_end`. 온도 ≤ MIN → 습도 원인 즉시 해제(0개면 `auto_vent_cold`). 누적 12시간 → `auto_vent_cap` |
| OFF 관측(R6) | 소유 중 `fan == 'OFF'` 2샘플: 타이머 만료(마지막 켬 + 3570초 이후)면 이어서 재발행, 그 전이면 외부 끔 → 소유 해제 + 30분 쉼. 직전 명령 뒤 300초 안이면 기다림 |
| 남의 명령(R7) | 수동 `fan_off`(소유 중 `fan_toggle` 포함) → 소유 해제 + 30분 쉼. 그 밖의 팬 명령 → 소유 해제만 |
| 예약(R8) | 구간 예약 켜짐 중엔 시작·종료·재발행 없음, 소유 해제 |
| 분무(R9) | 토글·`relay` 키·습도 < MIN 연속 180초·무장·`fan == 'OFF'`·이번 step 에 fan_on 없음·마지막 분무(모든 source) 뒤 2시간·오늘 자동 < 4 → `mist {duration_ms: 3000}`. 낸 순간 예산 소진(+1, 간격 기준, 무장 해제). 습도 ≥ MIN 샘플에서 재무장 |
| 토글 끔(R10) | 환기: 소유 중이면 `auto_vent_disabled` 후 환기 상태 초기화. 분무: 분무 상태만 초기화 |
| 재시도(R13) | 환기 명령을 낸 뒤 30초 안엔 같은 판정을 다시 내지 않는다(INSERT 실패 재시도 간격). 분무는 재시도 없음 |

습도 종료선 = `max(MAX − 5, (MIN + MAX) / 2)`, 온도 종료선 = MAX − 0.5. KST 날이 바뀌면 오늘 누적
환기 시간과 자동 분무 수를 0 으로 한다(wall 이 되감기면 날짜는 그대로 둔다).

## 연속 타이머

시작 판정 타이머(원인별 `over`)에는 "자동이 시작을 고려할 수 있음"(토글 켜짐·쉼 아님·예약 아님)도
함께 들어간다. 그래서 쉼·예약·습기 가드가 끝난 뒤엔 조건을 **다시 180초** 확인한다
(계획 테스트: 수동 끔 → 30분 + 180초 뒤 켬, 분무 → 60분 + 180초 뒤 켬). 반면 팬이 켜져 있음·600초
쉼·하루 상한·재시도 간격은 타이머를 끊지 않고 명령만 막는다(분무도 같음 — "팬이 켜져 있으면 보류,
연속 타이머는 유지").

## 호출측 약속

- `step()` 이 낸 명령을 INSERT(또는 섀도 로그)하면 `issued(cmd, mono)`, 실패하면 `insert_failed(cmd, mono)`.
- 자동 명령의 ACK 는 `on_own_ack`, 그 밖의 팬 명령 ACK ok 는 `on_other_ack`, source 무관 분무 ACK ok 는
  `on_mist_ok`. 기준(Thresholds)이 바뀌면 `reset_timers()`.
- `state_json()` 은 `updated_at` 말고는 전이 때만 바뀐다 — 호출측은 그걸 비교해 바뀔 때만 UPDATE 한다(R12).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from backend.env_rules import GAP_SEC, Thresholds

# ---------- 상수 (계획 Global Constraints 표가 정본) ----------

HOLD_SEC = 180                  # 시작 조건(> MAX / < MIN) 연속 성립 시간
VENT_HUMID_MARGIN = 5.0         # 습도 종료선 = max(MAX − 5, (MIN + MAX) / 2)
VENT_TEMP_MARGIN = 0.5          # 온도 종료선 = MAX − 0.5
END_HOLD_SEC = 60               # 종료선 이하 연속 60초 → 종료
VENT_DURATION_MS = 3_600_000    # 자동 fan_on 펌웨어 타이머(1시간)
REFIRE_MIN_SEC = 300            # 직전 자동 명령 뒤 이 시간 안엔 fan_on 재발행 안 함
VENT_MIN_OFF_SEC = 600          # 자동 환기 종료 뒤 다음 자동 시작까지
VENT_DAILY_MAX_SEC = 43_200     # KST 하루 자동 환기 누적 상한(12시간)
POST_MIST_GUARD_SEC = 3600      # 습기 신호 뒤 습도 원인 시작 금지
MANUAL_PAUSE_SEC = 1800         # 사용자 끔 뒤 자동 환기 쉼
MIST_DURATION_MS = 3000
MIST_MIN_INTERVAL_SEC = 7200    # 모든 source 마지막 분무 기준
MIST_DAILY_MAX = 4              # KST 하루 자동 분무 시도 수
OFF_STREAK = 2                  # 텔레메트리 fan == 'OFF' 연속 샘플 수(R6)
EXPIRY_SLACK_SEC = 30           # last_on + 3600 − 30 이후의 OFF 는 타이머 만료
RETRY_SEC = 30                  # 환기 명령 INSERT 실패(또는 미보고) 재시도 간격
STATE_RETRY_SEC = 60            # auto_env_state 쓰기 실패 재시도(store 가 씀)

# GAP_SEC(60) 은 env_rules 것을 그대로 쓴다(v4 D2 와 같은 공백 기준).

KST = timezone(timedelta(hours=9))
_UTC = timezone.utc

Cause = Literal["humid_high", "temp_high"]
CAUSES: tuple[Cause, ...] = ("humid_high", "temp_high")   # reason 에 붙는 순서(알파벳)
Phase = Literal["idle", "requested", "owned"]


def humid_end_line(th: Thresholds) -> float:
    """습도 원인 종료선. MAX−MIN 이 5 면 MAX−5 가 MIN 에 닿아 분무와 핑퐁하므로 중간값을 하한으로."""
    return max(th.humid_max - VENT_HUMID_MARGIN, (th.humid_min + th.humid_max) / 2)


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _kst_day(wall: datetime) -> date:
    if wall.tzinfo is None:     # naive 는 UTC 로 본다(서버 wall 은 aware 가 원칙)
        wall = wall.replace(tzinfo=_UTC)
    return wall.astimezone(KST).date()


def _held(since: float | None, now: float, sec: float) -> bool:
    return since is not None and now - since >= sec


# ---------- 입출력 ----------

@dataclass(frozen=True)
class AutoInput:
    """샘플 1건(R1). temp/humid 는 `a_ok` 가 거짓이거나 숫자가 아니면 None.

    fan: 텔레메트리 `fan` 값('ON'/'OFF', 키 없으면 None). has_fan/has_relay: 키 존재 여부(C4).
    vent_on/mist_on: 기기 토글. schedule_hold: 구간 예약 켜짐 중(R8, store 가 계산).
    wet_mono: 습기 사이클 감지 시각(래치된 최댓값, R3 ②). 분무 ACK(①)는 `on_mist_ok` 로 따로 받는다.
    """

    mono: float
    wall: datetime
    temp: float | None
    humid: float | None
    fan: str | None
    has_fan: bool
    has_relay: bool
    th: Thresholds
    vent_on: bool
    mist_on: bool
    schedule_hold: bool
    wet_mono: float | None


@dataclass(frozen=True)
class AutoCmd:
    """트래커가 내는 명령 의도. store 가 `commands` 에 source='auto' 로 넣는다(reason 은 C2 표)."""

    action: Literal["fan_on", "fan_off", "mist"]
    payload: dict[str, Any]
    reason: str


def _vent_on_cmd(causes: Iterable[str]) -> AutoCmd:
    reason = "auto_vent:" + "+".join(c for c in CAUSES if c in set(causes))
    return AutoCmd("fan_on", {"duration_ms": VENT_DURATION_MS}, reason)


def _vent_off_cmd(reason: str) -> AutoCmd:
    return AutoCmd("fan_off", {}, reason)


# ---------- 트래커 ----------

@dataclass
class AutoEnvTracker:
    """기기 하나의 자동 환기·분무 상태. 시각 필드는 전부 mono 초.

    환기 phase: idle(자동 소유 없음) → requested(fan_on INSERT 됨, ACK 대기) → owned(ACK ok).
    runtime: `_runtime_done`(오늘 끝난 자동 환기 누적) + `_run_acc`(지금 소유 중인 환기의 누적).
    """

    # ----- 연속 타이머 -----
    _over: dict[str, float] = field(default_factory=dict)     # 원인별 시작 조건 연속 시작
    _under: dict[str, float] = field(default_factory=dict)    # 원인별 종료선 이하 연속 시작
    _low_since: float | None = None                            # 습도 < MIN 연속 시작
    _last_mono: float | None = None                            # 직전 샘플(공백 판정)

    # ----- 환기 -----
    _phase: Phase = "idle"
    _causes: set[str] = field(default_factory=set)             # 서 있는 원인(소유·요청 중)
    _intent: frozenset[str] = frozenset()                      # 직전 step 이 낸 fan_on 의 원인
    _last_on: float | None = None                              # 마지막 자동 fan_on ACK ok
    _last_cmd: float | None = None                             # 마지막 자동 환기 명령 INSERT
    _retry_after: float | None = None                          # R13
    _paused_until: float | None = None
    _last_end: float | None = None
    _off_streak: int = 0
    _run_since: float | None = None                            # 이번 연속 자동 환기 시작(표시용)
    _run_acc: float = 0.0
    _acc_mark: float | None = None
    _runtime_done: float = 0.0
    _hold: bool = False                                        # 마지막 샘플의 schedule_hold

    # ----- 분무 -----
    _armed: bool = True
    _mist_last: float | None = None                            # 모든 source 마지막 분무(시도·ACK ok)
    _wet_ok: float | None = None                               # source 무관 분무 ACK ok(R3 ①)
    _mist_today: int = 0

    # ----- 날짜·표시 -----
    _day: date | None = None
    _ref: tuple[float, datetime] | None = None                 # 마지막 (mono, wall) — mono → wall 변환용
    _iso_cache: dict[str, tuple[float, str]] = field(default_factory=dict)

    # ===== 외부 조작 =====

    @property
    def phase(self) -> Phase:
        """환기 소유 단계(읽기 전용). store 가 "토글 꺼짐 + 소유 없음 → DB 0회" 판정에 쓴다."""
        return self._phase

    def reset_timers(self) -> None:
        """연속 타이머 전부 초기화(기준 변경, R1). 소유·예산·쉼은 그대로."""
        self._over.clear()
        self._under.clear()
        self._low_since = None
        self._last_mono = None

    def issued(self, cmd: AutoCmd, mono: float) -> None:
        """INSERT 성공(또는 섀도 로그). 분무는 step 에서 예산을 이미 썼으므로 할 일 없음."""
        if cmd.action == "fan_on":
            self._phase = "requested"
            self._causes = set(self._intent) or set(self._causes)
            self._last_cmd = mono
            self._retry_after = None
        elif cmd.action == "fan_off":
            self._last_cmd = mono
            self._release(mono, end=True)
            if cmd.reason == "auto_vent_disabled":
                self._reset_vent()

    def insert_failed(self, cmd: AutoCmd, mono: float) -> None:
        """INSERT 실패(R13). 환기는 상태 그대로 30초 뒤 같은 판정, 분무는 재시도 없음."""
        if cmd.action in ("fan_on", "fan_off"):
            self._retry_after = mono + RETRY_SEC

    def on_own_ack(self, action: str, ok: bool, mono: float) -> None:
        """자동 명령의 ACK(R4). fan_on ok 일 때만 소유 확정. 실패는 소유 없음(재판정은 300초 뒤)."""
        if action != "fan_on" or self._phase != "requested":
            return   # fan_off 는 issued 때 이미 소유를 놓았다. mist 는 on_mist_ok 로 따로.
        if ok:
            self._phase = "owned"
            self._last_on = mono
            self._acc_mark = mono
            self._off_streak = 0
            if self._run_since is None:
                self._run_since = mono
        else:
            self._release(mono)

    def on_other_ack(self, action: str, source: str, mono: float) -> None:
        """자동이 아닌 명령의 ACK ok(R7). 팬 명령이 아니면 무시."""
        if action not in ("fan_on", "fan_off", "fan_toggle"):
            return
        if source == "manual":
            if action == "fan_off" or (action == "fan_toggle" and self._phase == "owned"):
                self._release(mono)
                self._paused_until = mono + MANUAL_PAUSE_SEC
            elif action == "fan_on":
                self._release(mono)
            return
        self._release(mono)    # schedule·restore 등 — 소유 해제만

    def on_mist_ok(self, mono: float) -> None:
        """모든 source 의 분무 ACK ok(R3 ①·R9 간격)."""
        self._wet_ok = mono if self._wet_ok is None else max(self._wet_ok, mono)
        self._mist_last = mono if self._mist_last is None else max(self._mist_last, mono)

    def restore(self, *, mono: float, wall: datetime, day: date | None = None,
                paused_until: float | None = None, last_end: float | None = None,
                runtime_today_sec: float = 0.0, mist_today: int = 0,
                mist_last: float | None = None, owned_on: float | None = None,
                run_since: float | None = None, causes: Iterable[str] = ()) -> None:
        """브리지 재시작 복원(R11). 시각 인자는 호출측이 wall → mono 로 바꿔 넘긴다.

        day: `auto_env_state.day`. 오늘(KST)과 다르면 runtime·mist_today 는 0. None 이면 오늘 값으로 본다.
        mist_last: source 무관 마지막 분무 ACK ok — 간격(R9)과 습기 가드(R3 ①) 둘 다에 쓴다.
        owned_on: 마지막 작성자 판정을 통과한 자동 fan_on 의 시각(None = 소유 없음).
        run_since: 소유 복원 때 이번 연속 자동 환기 시작(`vent.since`). 없으면 owned_on.
        causes: 소유 복원 때의 원인(명령 reason 에서). 비면 두 원인 다 서 있다고 본다(종료가 더 보수적).
        """
        self.reset_timers()
        self._ref = (mono, wall)
        self._iso_cache.clear()
        today = _kst_day(wall)
        same_day = day is None or day == today
        self._day = today
        self._runtime_done = float(runtime_today_sec) if same_day else 0.0
        self._mist_today = int(mist_today) if same_day else 0
        self._paused_until = paused_until
        self._last_end = last_end
        self._mist_last = mist_last
        self._wet_ok = mist_last
        self._armed = True
        self._off_streak = 0
        self._retry_after = None
        self._intent = frozenset()
        if owned_on is None:
            self._phase = "idle"
            self._causes = set()
            self._last_on = self._run_since = self._acc_mark = None
            self._run_acc = 0.0
            return
        self._phase = "owned"
        self._causes = {c for c in causes if c in CAUSES} or set(CAUSES)
        self._last_on = owned_on
        self._last_cmd = owned_on
        self._run_since = owned_on if run_since is None else run_since
        # state 의 runtime 은 끝난 환기만 담으므로 지금 도는 환기를 더한다(오늘 0시 이후만).
        midnight = datetime.combine(today, datetime.min.time(), KST)
        since_midnight = (wall - midnight).total_seconds()
        self._run_acc = max(0.0, min(mono - self._run_since, since_midnight))
        self._acc_mark = mono

    # ===== 판정 =====

    def step(self, inp: AutoInput) -> list[AutoCmd]:
        """샘플 1건 판정. 한 step 에 환기 명령 ≤ 1, 분무 ≤ 1(같은 step 에 fan_on 이면 분무 없음)."""
        now = inp.mono
        self._ref = (now, inp.wall)
        self._roll_day(inp.wall, now)

        # 소유 중 누적(간격 > 60초면 60초까지만)
        if self._phase == "owned" and self._acc_mark is not None:
            self._run_acc += min(max(now - self._acc_mark, 0.0), GAP_SEC)
        if self._phase == "owned":
            self._acc_mark = now

        gap = self._last_mono is not None and now - self._last_mono > GAP_SEC
        if gap:
            self.reset_timers()
            self._off_streak = 0
        self._last_mono = now
        t, h = _num(inp.temp), _num(inp.humid)
        blind = gap or t is None or h is None    # R1: 이 샘플은 명령 없음

        fan = inp.fan.upper() if isinstance(inp.fan, str) else None
        self._off_streak = self._off_streak + 1 if fan == "OFF" else 0
        self._hold = inp.schedule_hold

        self._update_timers(inp, now, t, h)

        out: list[AutoCmd] = []
        vent = self._vent(inp, now, t, fan, blind)
        if vent is not None:
            self._retry_after = now + RETRY_SEC   # 호출측이 보고를 안 해도 30초 안엔 같은 의도 없음
            out.append(vent)
        mist = self._mist(inp, now, fan, blind, vent is not None and vent.action == "fan_on")
        if mist is not None:
            out.append(mist)
        return out

    def _roll_day(self, wall: datetime, now: float) -> None:
        d = _kst_day(wall)
        if self._day is None:
            self._day = d
            return
        if d <= self._day:
            return   # 같은 날, 또는 wall 되감김 — 날짜는 앞으로만
        self._day = d
        self._runtime_done = 0.0
        self._run_acc = 0.0
        self._mist_today = 0
        if self._phase == "owned":
            self._run_since = now   # 표시용: 오늘 몫만 since 부터 더하면 되도록

    def _update_timers(self, inp: AutoInput, now: float, t: float | None, h: float | None) -> None:
        th = inp.th
        paused = self._paused_until is not None and now < self._paused_until
        gate = inp.vent_on and not paused and not inp.schedule_hold
        guard_ok = self._guard_ok(inp, now)
        over = {
            "temp_high": gate and t is not None and t > th.temp_max,
            "humid_high": (gate and h is not None and h > th.humid_max
                           and t is not None and t > th.temp_min and guard_ok),
        }
        under = {
            "temp_high": t is not None and t <= th.temp_max - VENT_TEMP_MARGIN,
            "humid_high": h is not None and h <= humid_end_line(th),
        }
        for timers, conds in ((self._over, over), (self._under, under)):
            for c, ok in conds.items():
                if not ok:
                    timers.pop(c, None)
                else:
                    timers.setdefault(c, now)

        if not inp.mist_on:
            self._low_since = None
            self._armed = True        # R10: 분무 상태만 초기화(오늘 수·마지막 분무는 안전 예산이라 유지)
        elif h is None:
            self._low_since = None
        elif h < th.humid_min:
            if self._low_since is None:
                self._low_since = now
        else:
            self._low_since = None
            self._armed = True        # 회복 → 재무장

    def _guard_ok(self, inp: AutoInput, now: float) -> bool:
        wet = max((w for w in (inp.wet_mono, self._wet_ok) if w is not None), default=None)
        return wet is None or now - wet >= POST_MIST_GUARD_SEC

    def _runtime(self) -> float:
        return self._runtime_done + self._run_acc

    def _vent(self, inp: AutoInput, now: float, t: float | None, fan: str | None,
              blind: bool) -> AutoCmd | None:
        retry_wait = self._retry_after is not None and now < self._retry_after

        # R8: 예약 켜짐 중 — 아무 명령 없이 소유만 놓는다
        if inp.schedule_hold:
            if self._phase != "idle":
                self._release(now)
            return None

        # R10: 환기 토글 끔
        if not inp.vent_on:
            if self._phase == "owned":
                if blind or retry_wait:
                    return None
                return _vent_off_cmd("auto_vent_disabled")
            self._reset_vent()
            return None

        if self._phase == "requested":
            # ACK 는 TTL·no_ack 로 1분 안에 오지만, 못 받았을 때 영원히 묶이지 않게
            if self._last_cmd is not None and now - self._last_cmd < REFIRE_MIN_SEC:
                return None
            self._release(now)

        if self._phase == "owned":
            return self._vent_owned(inp, now, t, blind, retry_wait)

        # ----- idle: 시작(R4) -----
        if blind or retry_wait or not inp.has_fan or fan != "OFF":
            return None
        if self._last_end is not None and now - self._last_end < VENT_MIN_OFF_SEC:
            return None
        if self._last_cmd is not None and now - self._last_cmd < REFIRE_MIN_SEC:
            return None
        if self._runtime() >= VENT_DAILY_MAX_SEC:
            return None
        causes = [c for c in CAUSES if _held(self._over.get(c), now, HOLD_SEC)]
        if not causes:
            return None
        self._intent = frozenset(causes)
        return _vent_on_cmd(causes)

    def _vent_owned(self, inp: AutoInput, now: float, t: float | None, blind: bool,
                    retry_wait: bool) -> AutoCmd | None:
        th = inp.th
        # 소유 중 새로 성립한 원인은 합류(종료는 서 있는 원인 전부가 풀려야)
        for c in CAUSES:
            if _held(self._over.get(c), now, HOLD_SEC):
                self._causes.add(c)

        # R6: 소유 중 OFF 관측
        recent_cmd = self._last_cmd is not None and now - self._last_cmd < REFIRE_MIN_SEC
        if self._off_streak >= OFF_STREAK and not recent_cmd:
            assert self._last_on is not None
            expiry = self._last_on + VENT_DURATION_MS / 1000 - EXPIRY_SLACK_SEC
            if now < expiry:
                self._release(now)                          # ⓑ 외부 끔
                self._paused_until = now + MANUAL_PAUSE_SEC
                return None
            if retry_wait:
                return None
            refire = [
                c for c in CAUSES if c in self._causes
                and (c != "humid_high" or (t is not None and t > th.temp_min
                                           and self._guard_ok(inp, now)))
            ]
            if (blind or not refire or not inp.has_fan
                    or self._runtime() >= VENT_DAILY_MAX_SEC):
                self._release(now, end=True)                # ⓐ 이어 켤 수 없음 → 타이머 만료로 종료
                return None
            self._intent = frozenset(refire)
            return _vent_on_cmd(refire)                     # ⓐ 이어서 켜기(600초 쉼 없음)

        if blind or retry_wait:
            return None
        if self._runtime() >= VENT_DAILY_MAX_SEC:
            return _vent_off_cmd("auto_vent_cap")
        assert t is not None
        if t <= th.temp_min:
            self._causes.discard("humid_high")
            if not self._causes:
                return _vent_off_cmd("auto_vent_cold")
        if all(_held(self._under.get(c), now, END_HOLD_SEC) for c in self._causes):
            return _vent_off_cmd("auto_vent_end")
        return None

    def _mist(self, inp: AutoInput, now: float, fan: str | None, blind: bool,
              fan_on_now: bool) -> AutoCmd | None:
        if not inp.mist_on or blind or fan_on_now or not inp.has_relay:
            return None
        # 자동 환기 요청·소유 중이면 팬이 곧(또는 이미) 켜진다 — 텔레메트리 지연과 무관하게 보류
        if fan != "OFF" or self._phase != "idle":
            return None
        if not self._armed or not _held(self._low_since, now, HOLD_SEC):
            return None
        if self._mist_last is not None and now - self._mist_last < MIST_MIN_INTERVAL_SEC:
            return None
        if self._mist_today >= MIST_DAILY_MAX:
            return None
        # 시도 순간 예산 소진 — INSERT 결과와 무관, 재시도 없음
        self._mist_today += 1
        self._mist_last = now
        self._armed = False
        return AutoCmd("mist", {"duration_ms": MIST_DURATION_MS}, "auto_mist")

    # ===== 내부 상태 조작 =====

    def _release(self, now: float, *, end: bool = False) -> None:
        """자동 소유(또는 요청) 해제. end 면 자동 종료로 기록(600초 쉼 기준)."""
        self._runtime_done += self._run_acc
        self._run_acc = 0.0
        self._phase = "idle"
        self._causes = set()
        self._last_on = None
        self._acc_mark = None
        self._run_since = None
        self._off_streak = 0
        if end:
            self._last_end = now

    def _reset_vent(self) -> None:
        """R10 환기 상태 초기화. 오늘 누적·마지막 종료·마지막 명령은 안전값이라 남긴다."""
        self._release(0.0)
        self._over.clear()
        self._under.clear()
        self._retry_after = None
        self._paused_until = None
        self._intent = frozenset()

    # ===== 상태 노출(R12) =====

    def _iso(self, key: str, mono: float | None) -> str | None:
        """mono → UTC ISO(초 단위). 같은 mono 는 처음 계산한 문자열을 재사용 — 샘플마다 wall·mono
        오프셋이 μs 단위로 흔들려도 내용이 안 바뀌게(바뀔 때만 UPDATE)."""
        if mono is None or self._ref is None:
            return None
        hit = self._iso_cache.get(key)
        if hit is not None and hit[0] == mono:
            return hit[1]
        ref_mono, ref_wall = self._ref
        wall = ref_wall + timedelta(seconds=mono - ref_mono)
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=_UTC)
        s = wall.astimezone(_UTC).replace(microsecond=0).isoformat()
        self._iso_cache[key] = (mono, s)
        return s

    def state_json(self, wall: datetime) -> dict[str, Any]:
        """`devices.auto_env_state` 내용(C3). `updated_at` 말고는 전이 때만 바뀐다.

        runtime_today_sec 는 **끝난** 자동 환기의 오늘 누적이다. 소유 중이면 앱이 `since` 부터 지금까지를
        더한다(샘플마다 늘면 매 샘플 UPDATE 가 되므로). paused_until 은 지난 시각이면 null.
        """
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=_UTC)
        owned = self._phase == "owned"
        paused = self._iso("paused_until", self._paused_until)
        if paused is not None and datetime.fromisoformat(paused) <= wall:
            paused = None
        day = self._day or _kst_day(wall)
        return {
            "vent": {
                "active": owned,
                "causes": [c for c in CAUSES if c in self._causes] if owned else [],
                "since": self._iso("since", self._run_since) if owned else None,
                "paused_until": paused,
                "schedule_hold": self._hold,
                "last_end_at": self._iso("last_end", self._last_end),
                "runtime_today_sec": int(self._runtime_done),
            },
            "mist": {
                "last_at": self._iso("mist_last", self._mist_last),
                "today_count": self._mist_today,
            },
            "day": day.isoformat(),
            "updated_at": wall.astimezone(_UTC).replace(microsecond=0).isoformat(),
        }


__all__ = [
    "CAUSES",
    "END_HOLD_SEC",
    "EXPIRY_SLACK_SEC",
    "HOLD_SEC",
    "KST",
    "MANUAL_PAUSE_SEC",
    "MIST_DAILY_MAX",
    "MIST_DURATION_MS",
    "MIST_MIN_INTERVAL_SEC",
    "OFF_STREAK",
    "POST_MIST_GUARD_SEC",
    "REFIRE_MIN_SEC",
    "RETRY_SEC",
    "STATE_RETRY_SEC",
    "VENT_DAILY_MAX_SEC",
    "VENT_DURATION_MS",
    "VENT_HUMID_MARGIN",
    "VENT_MIN_OFF_SEC",
    "VENT_TEMP_MARGIN",
    "AutoCmd",
    "AutoEnvTracker",
    "AutoInput",
    "humid_end_line",
]
