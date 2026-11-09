"""auto_env — Stage R 자동 환기·분무 순수 상태 기계 단위 테스트.

계획: specs/stage-r-auto-env.md Task 1 (규칙 R1~R13, Review Focus 1·2·3·5).
시각은 monotonic 초(float)를 직접 넣고, wall 은 `T0 + mono` 로 맞춘다(KST 날짜 판정용).
`Rig(sim=True)` 는 기기를 흉내 낸다 — 명령은 곧바로 INSERT 성공 + ACK ok, `fan_on` 은 1시간 타이머로
켜지고 `fan_off` 는 끈다. 샘플에 `fan=` 을 직접 주면 모의 팬 대신 그 값을 쓴다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from backend.auto_env import (
    HOLD_SEC,
    HUMID_HOLD_SEC,
    MIST_DAILY_MAX,
    MIST_MIN_INTERVAL_SEC,
    VENT_DAILY_MAX_SEC,
    AutoCmd,
    AutoEnvTracker,
    AutoInput,
    humid_end_line,
)
from backend.env_rules import Thresholds

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=KST)   # 계획의 t0
TH = Thresholds(18.0, 27.0, 40.0, 80.0)          # 기본 기준(18~27℃ / 40~80%)

FAN_ON = AutoCmd("fan_on", {"duration_ms": 3_600_000}, "auto_vent:humid_high")
FAN_ON_T = AutoCmd("fan_on", {"duration_ms": 3_600_000}, "auto_vent:temp_high")
H = HUMID_HOLD_SEC                              # 습도 원인 연속 시간(2시간)
MIST = AutoCmd("mist", {"duration_ms": 3000}, "auto_mist")


class Rig:
    """트래커 + 시계(+ 모의 기기). `log` 에 (mono, AutoCmd) 를 쌓는다."""

    def __init__(self, sim: bool = False, t0: datetime = T0) -> None:
        self.tr = AutoEnvTracker()
        self.t0 = t0
        self.now = 0.0
        self.sim = sim
        self.fan_until: float | None = None   # 모의 팬: 이 mono 전까지 ON
        self.log: list[tuple[float, AutoCmd]] = []

    def wall(self, mono: float | None = None) -> datetime:
        return self.t0 + timedelta(seconds=self.now if mono is None else mono)

    def sim_fan(self) -> str:
        return "ON" if self.fan_until is not None and self.now < self.fan_until else "OFF"

    def run(self, secs: float, step: float = 3, **sample) -> list[AutoCmd]:
        """[now, now+secs) 구간을 step 간격 샘플로 흘려 넣고 나온 명령 전부. 끝나면 now = now+secs."""
        end = self.now + secs
        out: list[AutoCmd] = []
        while self.now < end:
            kw = dict(temp=24.0, humid=60.0, has_fan=True, has_relay=True, th=TH,
                      vent_on=True, mist_on=True, schedule_hold=False)
            kw["fan"] = self.sim_fan() if self.sim else "OFF"
            kw.update(sample)
            cmds = self.tr.step(AutoInput(mono=self.now, wall=self.wall(), **kw))
            for c in cmds:
                self.log.append((self.now, c))
                if self.sim:
                    self.tr.issued(c, self.now)
                    if c.action == "fan_on":
                        self.tr.on_own_ack("fan_on", True, self.now)
                        self.fan_until = self.now + c.payload["duration_ms"] / 1000
                    elif c.action == "fan_off":
                        self.tr.on_own_ack("fan_off", True, self.now)
                        self.fan_until = None
                    else:
                        self.tr.on_mist_ok(self.now)
            out += cmds
            self.now += step
        self.now = end
        return out

    def times(self, action: str, reason: str | None = None) -> list[float]:
        return [t for t, c in self.log if c.action == action and (reason is None or c.reason == reason)]


def own(r: Rig, **sample) -> None:
    """습도 85 로 2시간 → 자동 fan_on(humid_high) + ACK ok(모의 기기). 끝나면 now = H + 3."""
    assert r.sim
    assert r.run(H + 3, humid=85.0, **sample) == [FAN_ON]
    assert r.tr.state_json(r.wall())["vent"]["active"] is True


def own_t(r: Rig, **sample) -> None:
    """온도 28 로 180초 → 자동 fan_on(temp_high) + ACK ok(모의 기기). 끝나면 now = 183.

    소유·ACK·쉼·예약처럼 원인 종류와 무관한 규칙은 이걸로 본다(습도 2시간을 기다리지 않게).
    """
    assert r.sim
    assert r.run(183, **{"temp": 28.0, **sample}) == [FAN_ON_T]
    assert r.tr.state_json(r.wall())["vent"]["active"] is True


# ---------- 시작 (R2·R4) ----------

def test_humid_high_needs_two_hours():
    """습도 원인 = MAX 초과 2시간 연속(v4 과습 주의와 같은 조건)."""
    assert H == 7200
    r = Rig()
    assert r.run(7200, humid=85.0) == []         # 0~7197초
    assert r.run(3, humid=85.0) == [FAN_ON]      # 7200초째


def test_humid_hold_resets_on_dip():
    r = Rig()
    assert r.run(3000, humid=85.0) == []         # 50분
    assert r.run(3, humid=79.0) == []            # MAX 이하 한 샘플 → 연속 끊김
    assert r.run(H, humid=85.0) == []            # 3003~10200초 — 다시 2시간 전
    assert r.run(3, humid=85.0) == [FAN_ON]      # 3003 + 7200


def test_manual_mist_no_longer_blocks_vent():
    """r3: 분무 뒤 60분 보호 없음 — 분무 ACK 직후부터 2시간이면 켠다(추가 대기 없음)."""
    r = Rig()
    r.tr.on_mist_ok(0.0)
    assert r.run(H, humid=85.0) == []
    assert r.run(3, humid=85.0) == [FAN_ON]


def test_temp_cause_still_three_minutes():
    r = Rig()
    assert r.run(180, temp=28.0) == []
    assert r.run(3, temp=28.0) == [FAN_ON_T]
    assert HOLD_SEC == 180


def test_cold_blocks_humid_vent():
    r = Rig()
    assert r.run(H + 600, humid=85.0, temp=17.5) == []

    r = Rig(sim=True)
    own(r)
    assert r.run(3, humid=85.0, temp=18.0) == [AutoCmd("fan_off", {}, "auto_vent_cold")]
    assert r.tr.state_json(r.wall())["vent"]["active"] is False


def test_no_flap_near_max():
    """Review 1 — MAX 근처에서 켜짐·꺼짐이 반복되지 않는다."""
    r = Rig(sim=True)
    r.run(H + 3, humid=81.0)                # MAX+1 → 2시간째 on
    assert r.run(600, humid=79.0) == []     # MAX−1 10분 → 종료선(75) 위라 off 없음
    r.run(63, humid=74.0)                   # 종료선 아래 60초 → off 1회
    end = r.now - 3
    assert r.times("fan_off", "auto_vent_end") == [end]
    r.run(H + 3, humid=81.0)                # 다시 MAX+1 — 2시간 연속을 처음부터
    assert r.times("fan_on") == [float(H), end + 3 + H]
    assert len(r.times("fan_off")) == 1

    # 600초 쉼은 온도 원인(180초)으로 본다 — 습도는 2시간 연속이 쉼보다 길다
    r = Rig(sim=True)
    own_t(r, temp=27.5)
    r.run(63, temp=26.0)                    # 온도 종료선(26.5) 아래 60초 → off
    end = r.now - 3
    assert r.times("fan_off", "auto_vent_end") == [end]
    r.run(900, temp=27.5)
    assert r.times("fan_on") == [180.0, end + 600]   # 600초 쉼 안엔 on 없음


def test_humid_end_line_midpoint():
    th = Thresholds(18.0, 27.0, 75.0, 80.0)
    assert humid_end_line(th) == 77.5                 # max(80−5, (75+80)/2)
    assert humid_end_line(TH) == 75.0                 # max(75, 60)
    r = Rig(sim=True)
    own(r, th=th)
    assert r.run(120, humid=78.0, th=th) == []        # 종료선 위
    assert r.run(60, humid=76.0, th=th) == []         # 57초까지
    assert r.run(3, humid=76.0, th=th) == [AutoCmd("fan_off", {}, "auto_vent_end")]


def test_temp_cause_keeps_running():
    r = Rig(sim=True)
    assert r.run(H - HOLD_SEC, humid=85.0) == []
    assert r.run(183, humid=85.0, temp=28.0) == [     # 두 원인이 같은 샘플(7200초)에 성립
        AutoCmd("fan_on", {"duration_ms": 3_600_000}, "auto_vent:humid_high+temp_high")
    ]
    assert r.run(600, humid=70.0, temp=28.0) == []     # 습도는 해소, 온도 원인이 남음
    assert r.run(60, humid=70.0, temp=26.4) == []
    assert r.run(3, humid=70.0, temp=26.4) == [AutoCmd("fan_off", {}, "auto_vent_end")]


def test_fan_on_by_user_no_command():
    r = Rig()
    assert r.run(H + 600, humid=85.0, temp=28.0, fan="ON") == []
    assert r.run(600, humid=60.0, fan="ON") == []


def test_ownership_needs_ack():
    # issued 만 하고 ACK 없음 → 소유 아님 → 원인이 풀려도 fan_off 없음
    r = Rig()
    assert r.run(183, temp=28.0) == [FAN_ON_T]
    r.tr.issued(FAN_ON_T, 180.0)
    assert r.run(1200, temp=24.0) == []
    assert r.tr.state_json(r.wall())["vent"]["active"] is False

    # ACK ok 뒤에만 fan_off
    r = Rig()
    r.run(183, temp=28.0)
    r.tr.issued(FAN_ON_T, 180.0)
    r.tr.on_own_ack("fan_on", True, 181.0)
    assert r.run(60, temp=24.0) == []
    assert r.run(3, temp=24.0, fan="ON") == [AutoCmd("fan_off", {}, "auto_vent_end")]

    # ACK 실패 → 소유 없음, 300초 뒤 재판정
    r = Rig()
    r.run(183, temp=28.0)
    r.tr.issued(FAN_ON_T, 180.0)
    r.tr.on_own_ack("fan_on", False, 181.0)
    assert r.run(480 - 183, temp=28.0) == []
    assert r.run(3, temp=28.0) == [FAN_ON_T]


# ---------- 사용자 조작 (Review 2, R6·R7) ----------

def test_manual_off_pauses_30min():
    r = Rig(sim=True)
    own_t(r)
    r.run(600 - 183, temp=28.0)
    r.tr.on_other_ack("fan_off", "manual", 600.0)
    r.fan_until = None
    st = r.tr.state_json(r.wall())["vent"]
    assert st["active"] is False and st["paused_until"] is not None
    assert r.run(1800 + HOLD_SEC, temp=28.0) == []        # 30분 쉼 + 다시 180초
    assert r.run(3, temp=28.0) == [FAN_ON_T]
    assert r.times("fan_on") == [180.0, 600.0 + 1800 + HOLD_SEC]

    # fan_toggle 도 소유 중이면 같음
    r = Rig(sim=True)
    own_t(r)
    r.run(600 - 183, temp=28.0)
    r.tr.on_other_ack("fan_toggle", "manual", 600.0)
    r.fan_until = None
    assert r.run(1800 + HOLD_SEC, temp=28.0) == []
    assert r.run(3, temp=28.0) == [FAN_ON_T]

    # 습도 원인이면 쉼 뒤 다시 2시간
    r = Rig(sim=True)
    own(r)
    r.tr.on_other_ack("fan_off", "manual", r.now)
    r.fan_until = None
    paused_end = r.now + 1800
    assert r.run(1800 + H, humid=85.0) == []
    assert r.run(3, humid=85.0) == [FAN_ON]
    assert r.times("fan_on") == [float(H), paused_end + H]

    # 비소유(사용자가 켠 팬)면 쉼 없음 — 끄자마자 자동이 켠다
    r = Rig()
    assert r.run(600, temp=28.0, fan="ON") == []
    r.tr.on_other_ack("fan_toggle", "manual", 600.0)
    assert r.run(3, temp=28.0) == [FAN_ON_T]


def test_expiry_refire():
    r = Rig()
    assert r.run(H + 3, humid=85.0) == [FAN_ON]
    on = float(H)
    r.tr.issued(FAN_ON, on)
    r.tr.on_own_ack("fan_on", True, on)
    assert r.run(on + 3570 - r.now, humid=85.0, fan="ON") == []   # 켬 + 3570초(= 3600 − 30)까지 켜짐
    assert r.run(3, humid=85.0, fan="OFF") == []            # OFF 1샘플째
    assert r.run(3, humid=85.0, fan="OFF") == [FAN_ON]      # 2샘플째 → 같은 reason, 600초 쉼 없음
    r.tr.issued(FAN_ON, r.now - 3)
    r.tr.on_own_ack("fan_on", True, r.now - 2)
    assert r.tr.state_json(r.wall())["vent"]["active"] is True


def test_expiry_refire_needs_hold():
    """타이머 만료 재발행도 원인별 연속 시간을 다시 본다 — 습도가 MAX 아래(종료선 위)로 내려왔으면
    이어 켜지 않고 끝낸다(습도 원인 재시작은 다시 2시간)."""
    r = Rig()
    assert r.run(H + 3, humid=85.0) == [FAN_ON]
    on = float(H)
    r.tr.issued(FAN_ON, on)
    r.tr.on_own_ack("fan_on", True, on)
    r.run(on + 3570 - r.now - 300, humid=85.0, fan="ON")
    assert r.run(300, humid=78.0, fan="ON") == []           # MAX 이하·종료선(75) 위 — 종료 없음
    assert r.run(6, humid=78.0, fan="OFF") == []            # 만료 OFF 2샘플 → 재발행 없이 종료
    st = r.tr.state_json(r.wall())["vent"]
    assert st["active"] is False and st["last_end_at"] is not None and st["paused_until"] is None


def test_external_off_before_expiry_pauses():
    r = Rig()
    r.run(183, temp=28.0)
    r.tr.issued(FAN_ON_T, 180.0)
    r.tr.on_own_ack("fan_on", True, 180.0)
    r.run(1380 - 183, temp=28.0, fan="ON")                  # 소유 후 20분
    assert r.run(6, temp=28.0, fan="OFF") == []             # OFF 2샘플 → 외부 끔
    st = r.tr.state_json(r.wall())["vent"]
    assert st["active"] is False and st["paused_until"] is not None
    assert r.run(1800 + HOLD_SEC - 3, temp=28.0) == []      # 1383 + 1800 + 180 전까지
    assert r.run(3, temp=28.0) == [FAN_ON_T]


# ---------- 예약 (Review 5, R8) ----------

def test_schedule_hold_blocks_all():
    # 시작 없음
    r = Rig()
    assert r.run(H + 600, humid=85.0, temp=28.0, schedule_hold=True) == []
    assert r.tr.state_json(r.wall())["vent"]["schedule_hold"] is True

    # 소유 중 예약 켜짐 → 소유 해제, 종료·재발행 없음
    r = Rig(sim=True)
    own_t(r)
    assert r.run(3, temp=28.0, schedule_hold=True, fan="ON") == []
    st = r.tr.state_json(r.wall())["vent"]
    assert st["schedule_hold"] is True and st["active"] is False
    assert r.run(600, temp=24.0, schedule_hold=True, fan="ON") == []         # 종료 없음
    assert r.run(7200, temp=28.0, schedule_hold=True, fan="OFF") == []       # 재발행·시작 없음
    # 예약이 끝나면(fan_off) 3분 뒤 다시 켤 수 있다(온도 원인)
    assert r.run(180, temp=28.0) == []
    assert r.run(3, temp=28.0) == [FAN_ON_T]
    assert r.tr.state_json(r.wall())["vent"]["schedule_hold"] is False


# ---------- 하루 상한 (Review 3, R5) ----------

def test_vent_daily_cap():
    t0 = datetime(2026, 11, 5, 0, 30, tzinfo=KST)   # 12시간 상한이 같은 KST 날 안에 오도록
    r = Rig(sim=True, t0=t0)
    end_of_day = (datetime(2026, 11, 5, 23, 59, tzinfo=KST) - t0).total_seconds()
    r.run(end_of_day, step=30, humid=99.0)
    caps = r.times("fan_off", "auto_vent_cap")
    assert len(caps) == 1
    first_on = r.times("fan_on")[0]
    assert caps[0] - first_on >= VENT_DAILY_MAX_SEC
    assert caps[0] - first_on < VENT_DAILY_MAX_SEC + 60
    assert [t for t in r.times("fan_on") if t > caps[0]] == []      # 그날 재시작 없음
    assert len(r.times("fan_off")) == 1                               # 그 사이엔 타이머 만료 재발행만
    assert r.tr.state_json(r.wall())["vent"]["runtime_today_sec"] >= VENT_DAILY_MAX_SEC


# ---------- 자동 분무 (R9, Review 3) ----------

def test_mist_low_humid():
    r = Rig()
    assert r.run(180, humid=35.0) == []
    assert r.run(3, humid=35.0) == [MIST]


def test_mist_retries_while_dry():
    # 습도가 회복되지 않아도 2시간 간격·하루 4회로 다시 분무한다(물통 빔 → 하루 최대 12초)
    r = Rig(sim=True)
    r.run(24 * 3600, step=30, humid=30.0)
    mists = r.times("mist")
    days: dict[date, int] = {}
    for t in mists:
        d = r.wall(t).astimezone(KST).date()
        days[d] = days.get(d, 0) + 1
    assert days == {date(2026, 11, 5): MIST_DAILY_MAX, date(2026, 11, 6): MIST_DAILY_MAX}
    assert all(b - a >= MIST_MIN_INTERVAL_SEC for a, b in zip(mists, mists[1:]))


def test_mist_cap_with_recovery():
    t0 = datetime(2026, 11, 5, 0, 30, tzinfo=KST)
    r = Rig(sim=True, t0=t0)
    for _ in range(36):                       # 1시간마다 40분 건조 → 20분 회복
        r.run(2400, step=30, humid=35.0)
        r.run(1200, step=30, humid=50.0)
    mists = r.times("mist")
    days: dict[date, int] = {}
    for t in mists:
        d = r.wall(t).astimezone(KST).date()
        days[d] = days.get(d, 0) + 1
    assert days[date(2026, 11, 5)] == MIST_DAILY_MAX
    assert days[date(2026, 11, 6)] >= 1                    # KST 0시 리셋
    assert all(n <= MIST_DAILY_MAX for n in days.values())
    assert all(b - a >= MIST_MIN_INTERVAL_SEC for a, b in zip(mists, mists[1:]))


def test_mist_interval_counts_any_source():
    r = Rig()
    r.tr.on_mist_ok(0.0)                      # 수동 분무
    assert r.run(3600, humid=50.0) == []
    assert r.run(3600, humid=35.0) == []      # 1시간 뒤 건조 — 간격 2시간 전
    assert r.run(3, humid=35.0) == [MIST]     # 2시간째


def test_mist_needs_fan_off_known():
    r = Rig()
    assert r.run(600, humid=35.0, fan=None) == []
    r = Rig()
    assert r.run(600, humid=35.0, has_relay=False) == []
    r = Rig()
    assert r.run(600, humid=35.0, fan="ON") == []
    assert r.run(3, humid=35.0, fan="OFF") == [MIST]     # 연속 타이머 유지 → OFF 첫 샘플


def test_fan_on_and_mist_same_step():
    r = Rig()
    assert r.run(180, humid=35.0, temp=30.0) == []
    assert r.run(3, humid=35.0, temp=30.0) == [
        AutoCmd("fan_on", {"duration_ms": 3_600_000}, "auto_vent:temp_high")
    ]
    r.tr.issued(r.log[-1][1], 180.0)
    # 팬 요청 중(텔레메트리는 아직 OFF)에도 분무하지 않는다
    assert r.run(30, humid=35.0, temp=30.0) == []


# ---------- 입력·기준 (R1) ----------

def test_no_fan_key_no_vent():
    r = Rig()
    assert r.run(600, humid=85.0, temp=30.0, has_fan=False, fan=None) == []


def test_missing_or_gap_resets():
    r = Rig()
    assert r.run(120, temp=28.0) == []
    assert r.run(3, temp=None) == []           # 값 없음 → 그 값의 연속 타이머 초기화
    assert r.run(120, temp=28.0) == []

    r = Rig()
    assert r.run(123, temp=28.0) == []         # 0~120초
    r.now += 67                                # 다음 샘플까지 70초
    assert r.run(120, temp=28.0) == []

    r = Rig()                                  # 습도 원인도 공백이면 1시간을 처음부터
    assert r.run(3000, humid=85.0) == []
    r.now += 67                                # 3067초 샘플(공백 뒤)부터 다시 셈
    assert r.run(H, humid=85.0) == []
    assert r.run(3, humid=85.0) == [FAN_ON]    # 3067 + 3600


def test_threshold_change_resets():
    r = Rig()
    assert r.run(123, temp=28.0) == []         # 0~120초
    r.tr.reset_timers()
    assert r.run(180, temp=28.0) == []         # 123~300초
    assert r.run(3, temp=28.0) == [FAN_ON_T]   # 123 + 180


# ---------- 토글·INSERT 실패 (R10·R13) ----------

def test_toggle_off_sends_off_when_owned():
    r = Rig(sim=True)
    own_t(r)
    assert r.run(3, temp=28.0, vent_on=False) == [AutoCmd("fan_off", {}, "auto_vent_disabled")]
    assert r.run(600, temp=28.0, vent_on=False) == []
    assert r.tr.state_json(r.wall())["vent"]["active"] is False

    r = Rig()
    assert r.run(H + 600, humid=85.0, temp=28.0, vent_on=False) == []      # 비소유면 명령 없음


def test_insert_failed_retry_30s():
    r = Rig()
    assert r.run(183, temp=28.0) == [FAN_ON_T]
    r.tr.insert_failed(FAN_ON_T, 180.0)
    assert r.run(27, temp=28.0) == []          # 183~207
    assert r.run(3, temp=28.0) == [FAN_ON_T]   # 210

    r = Rig()
    assert r.run(183, humid=35.0) == [MIST]
    r.tr.insert_failed(MIST, 180.0)
    assert r.run(7197, humid=35.0) == []       # 183~7377 — 실패해도 간격 안엔 재시도 없음
    assert r.run(3, humid=35.0) == [MIST]      # 7380 — 2시간 뒤 정규 재시도


def test_kst_day_rollover():
    t0 = datetime(2026, 11, 5, 23, 50, tzinfo=KST)
    r = Rig(t0=t0)
    r.tr.restore(mono=0.0, wall=t0, day=date(2026, 11, 5), runtime_today_sec=100.0,
                 mist_today=MIST_DAILY_MAX)
    assert r.run(600, humid=35.0) == []        # 23:50~23:59:57 — 4회 소진
    assert r.run(3, humid=35.0) == [MIST]      # 00:00 KST
    st = r.tr.state_json(r.wall())
    assert st["day"] == "2026-11-06"
    assert st["mist"]["today_count"] == 1
    assert st["vent"]["runtime_today_sec"] == 0


# ---------- 상태 노출·복원 (R11·R12) ----------

def test_state_json_shape():
    r = Rig(sim=True)
    own(r)
    st = r.tr.state_json(r.wall())
    assert st["vent"] == {
        "active": True, "causes": ["humid_high"], "since": "2026-11-05T05:00:00+00:00",
        "paused_until": None, "schedule_hold": False, "last_end_at": None, "runtime_today_sec": 0,
    }
    assert st["mist"] == {"last_at": None, "today_count": 0}
    assert st["day"] == "2026-11-05"
    assert st["updated_at"] == "2026-11-05T05:00:03+00:00"
    # 샘플이 더 와도 updated_at 말고는 그대로(R12 — 바뀔 때만 UPDATE)
    r.run(300, humid=85.0)
    st2 = r.tr.state_json(r.wall())
    assert {k: v for k, v in st2.items() if k != "updated_at"} == \
        {k: v for k, v in st.items() if k != "updated_at"}


def test_restore_owned_ends_after_recovery():
    r = Rig()
    r.tr.restore(mono=0.0, wall=T0, owned_on=-2400.0, causes=["humid_high"])
    assert r.tr.state_json(r.wall())["vent"]["active"] is True
    assert r.run(60, humid=60.0, fan="ON") == []
    assert r.run(3, humid=60.0, fan="ON") == [AutoCmd("fan_off", {}, "auto_vent_end")]
