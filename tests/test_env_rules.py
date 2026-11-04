"""env_rules — v4 온습도 판정 상태 기계 단위 테스트.

계획: specs/stage-n-env-alerts-v4.md Task 1 (결정 D1~D16, 전이 우선순위 표).
시각은 전부 monotonic 초(float) 를 직접 넣는다 — 모듈이 시계를 안 부르는지도 같이 확인되는 셈.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from backend.env_rules import (
    REDANGER_SILENCE_SEC,
    ActiveEvent,
    ElementTracker,
    Thresholds,
    title,
)

TH = Thresholds(18.0, 26.0, 40.0, 80.0)   # 동작 테스트용 고정 기준 — 디폴트 값이 바뀌어도 그대로(디폴트는 아래 sanitize 테스트)

SKIP = object()  # seg() 에서 "이 구간은 샘플 없음(통신 공백)"


def seg(*parts: tuple[float, object], end: float) -> list[tuple[float, object]]:
    """[(시작, 값), ...] 구간을 3초 간격 샘플로 펼친다. 각 구간은 다음 시작(마지막은 end) 직전까지."""
    out: list[tuple[float, object]] = []
    bounds = [p[0] for p in parts[1:]] + [end]
    for (start, value), stop in zip(parts, bounds):
        if value is SKIP:
            continue
        t = start
        while t < stop:
            out.append((t, value))
            t += 3
    return out


def feed(tr: ElementTracker, samples, th: Thresholds = TH):
    """샘플을 순서대로 step 하고 (시각, Action) 목록을 돌려준다."""
    got = []
    for t, v in samples:
        for a in tr.step(t, v, th):
            got.append((t, a))
    return got


def ops(got, op: str):
    return [(t, a) for t, a in got if a.op == op]


def _open_high_caution(tr: ElementTracker, until: float = 210) -> None:
    """26.3℃ 를 0초부터 until 직전까지 → t=120 에 고온 주의 open. alert_id 'a1' 로 기록된 셈 친다."""
    got = feed(tr, seg((0, 26.3), end=until))
    assert [a.op for _, a in got] == ["open"]
    tr.active.alert_id = "a1"


# ---------- 진입 ----------

def test_caution_after_2min_continuous():
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 26.3), end=200))
    assert all(t >= 120 for t, _ in got)
    t, a = got[0]
    assert t == 120
    assert (a.op, a.kind, a.level, a.push) == ("open", "env_temp_high", "caution", True)
    assert a.at_mono == 0
    assert a.value == 26.3 and a.threshold == 26.0
    assert len(got) == 1


@pytest.mark.parametrize(
    ("element", "value"),
    [("temp", 26.0), ("temp", 18.0), ("humid", 80.0), ("humid", 40.0)],
)
def test_boundaries_exact(element, value):
    # 경계값은 적정(MIN ≤ 값 ≤ MAX)
    tr = ElementTracker(element)
    assert feed(tr, seg((0, value), end=600)) == []


@pytest.mark.parametrize(("value", "kind"), [(29.0, "env_temp_high"), (15.0, "env_temp_low")])
def test_boundaries_exact_not_danger(value, kind):
    # 29.0·15.0 은 경고가 아니라 주의
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, value), end=600))
    assert [(a.op, a.kind, a.level) for _, a in got] == [("open", kind, "caution")]


def test_jitter_on_boundary_resets_entry():
    # 26.1, 26.2, 26.0 반복 10분 → 26.0 이 낄 때마다 초기화(D1) → open 없음
    tr = ElementTracker("temp")
    cycle = [26.1, 26.2, 26.0]
    samples = [(t, cycle[(t // 3) % 3]) for t in range(0, 600, 3)]
    assert feed(tr, samples) == []


# ---------- 회복 ----------

def test_recovery_needs_5min_in_range():
    tr = ElementTracker("temp")
    _open_high_caution(tr, until=200)
    # 25.5 를 200초부터: 299초 유지 → 없음, 300초 → close(at == 적정 시작)
    assert feed(tr, seg((200, 25.5), end=499)) == []
    assert feed(tr, [(499, 25.5)]) == []
    got = feed(tr, [(500, 25.5)])
    assert len(got) == 1
    a = got[0][1]
    assert (a.op, a.kind, a.closes_alert_id, a.push) == ("close", "env_temp_high", "a1", False)
    assert a.at_mono == 200
    assert tr.active is None


def test_brief_return_keeps_event():
    tr = ElementTracker("temp")
    _open_high_caution(tr, until=200)
    # 적정 4분 후 재이탈 → 같은 이벤트 유지
    got = feed(tr, seg((200, 25.0), (440, 26.2), end=1200))
    assert ops(got, "close") == []
    assert ops(got, "open") == []
    assert tr.active is not None and tr.active.alert_id == "a1"


# ---------- 주의 ↔ 경고 ----------

def test_caution_to_danger():
    tr = ElementTracker("temp")
    _open_high_caution(tr, until=210)
    got = feed(tr, seg((210, 29.4), end=400))
    assert [t for t, _ in got] == [330, 330]
    close, open_ = got[0][1], got[1][1]
    assert (close.op, close.kind, close.level, close.closes_alert_id) == (
        "close", "env_temp_high", "caution", "a1")
    assert (open_.op, open_.kind, open_.level, open_.push) == (
        "open", "env_temp_high", "danger", True)
    assert close.at_mono == open_.at_mono == 210
    # D10: 경고도 threshold 는 사용자 MAX
    assert open_.threshold == 26.0
    # 닫히는 주의의 peak 에 경고 구간 값은 섞이지 않음
    assert close.peak == 26.3
    assert open_.peak == 29.4
    assert tr.active.level == "danger"


def test_low_side_symmetric():
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 17.5), end=200))
    assert [(a.op, a.kind, a.level, a.push) for _, a in got] == [
        ("open", "env_temp_low", "caution", True)]
    assert got[0][1].threshold == 18.0
    tr.active.alert_id = "c1"
    # 14.5 → 저온 경고
    got = feed(tr, seg((200, 14.5), end=400))
    assert [(a.op, a.kind, a.level) for _, a in got] == [
        ("close", "env_temp_low", "caution"), ("open", "env_temp_low", "danger")]
    assert got[1][1].push is True
    tr.active.alert_id = "d1"
    # 16.0 5분 → 무음 완화
    got = feed(tr, seg((400, 16.0), end=800))
    assert [(a.op, a.kind, a.level, a.push) for _, a in got] == [
        ("close", "env_temp_low", "danger", False), ("open", "env_temp_low", "caution", False)]
    assert got[0][1].at_mono == got[1][1].at_mono == 400
    tr.active.alert_id = "c2"
    # 20℃ 5분 → 회복
    got = feed(tr, seg((800, 20.0), end=1200))
    assert [(a.op, a.closes_alert_id) for _, a in got] == [("close", "c2")]
    assert got[0][1].at_mono == 800


def test_danger_from_normal_skips_caution():
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 24.0), (30, 30.0), end=400))
    assert [(t, a.op, a.kind, a.level) for t, a in got] == [
        (150, "open", "env_temp_high", "danger")]
    assert got[0][1].at_mono == 30


def test_caution_held_while_danger_building():
    # D13: 27℃ 60초(주의 조건 시작) → 30℃. 주의 2분째(t=150)엔 경고 조건 성립 중이라 보류,
    # 30℃ 진입 2분째(t=210) 경고 1건만.
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 24.0), (30, 27.0), (90, 30.0), end=600))
    assert [(t, a.op, a.level) for t, a in got] == [(210, "open", "danger")]


def test_danger_deescalates_silently():
    tr = ElementTracker("temp")
    feed(tr, seg((0, 30.0), end=300))
    tr.active.alert_id = "d1"
    got = feed(tr, seg((300, 27.5), end=700))
    assert [t for t, _ in got] == [600, 600]
    close, open_ = got[0][1], got[1][1]
    assert (close.op, close.level, close.closes_alert_id) == ("close", "danger", "d1")
    assert (open_.op, open_.kind, open_.level, open_.push) == (
        "open", "env_temp_high", "caution", False)
    assert close.at_mono == open_.at_mono == 300
    assert tr.active.silent is True
    # 무음 주의는 추가 푸시도 없음(푸시를 안 했으니 pushed_mono 도 없음)
    assert tr.active.pushed_mono is None
    assert ops(feed(tr, seg((700, 27.5), end=600 + 3 * 3600)), "remind") == []


def _deescalated_tracker() -> ElementTracker:
    """경고(0초~) → 27.5℃(300초~) → t=600 무음 주의(시작 시각 300)."""
    tr = ElementTracker("temp")
    feed(tr, seg((0, 30.0), end=300))
    got = feed(tr, seg((300, 27.5), end=603))
    assert [a.op for _, a in got] == ["close", "open"] and tr.active.silent
    return tr


def test_redanger_within_30min_silent():
    # 무음 주의 10분 뒤 29.4 2분 → 경고 open, 푸시 생략(D14)
    tr = _deescalated_tracker()
    got = feed(tr, seg((603, 27.5), (1200, 29.4), end=1400))
    opens = ops(got, "open")
    assert [(t, a.level, a.push) for t, a in opens] == [(1320, "danger", False)]
    assert tr.active.pushed_mono is None
    # 31분 뒤면 푸시
    tr = _deescalated_tracker()
    start = 600 + 31 * 60
    assert start - 300 > REDANGER_SILENCE_SEC
    got = feed(tr, seg((603, 27.5), (start, 29.4), end=start + 200))
    opens = ops(got, "open")
    assert [(a.level, a.push) for _, a in opens] == [("danger", True)]


def test_redanger_inherits_chain_reminder():
    # 경고(푸시) → 무음 주의 → 30분 안 재경고(푸시 생략): 추가 푸시는 처음 경고 푸시 +1시간에 1회
    tr = ElementTracker("temp")
    feed(tr, seg((0, 30.0), end=123))
    tr.active.alert_id = "d1"
    tr.mark_pushed(120)
    got = feed(tr, seg((123, 30.0), (300, 27.5), (1200, 29.4), end=8000))
    opens = ops(got, "open")
    assert [(t, a.level, a.push) for t, a in opens] == [(600, "caution", False), (1320, "danger", False)]
    assert tr.active.pushed_mono == 120
    assert [t for t, _ in ops(got, "remind")] == [3720]


def test_redanger_chain_already_reminded_no_second():
    # 처음 경고가 이미 추가 푸시를 보냈으면 체인 안의 재경고는 더 보내지 않는다
    tr = ElementTracker("temp")
    feed(tr, seg((0, 30.0), end=123))
    tr.active.alert_id = "d1"
    tr.mark_pushed(120)
    got = feed(tr, seg((123, 30.0), (3800, 27.5), (4200, 29.4), end=12000))
    assert [(t, a.level, a.push) for t, a in ops(got, "open")] == [
        (4100, "caution", False), (4320, "danger", False)]
    assert [t for t, _ in ops(got, "remind")] == [3720]


# ---------- 추가 푸시 ----------

def test_reminder_once_after_1h():
    tr = ElementTracker("temp")
    feed(tr, seg((0, 26.3), end=123))
    tr.mark_pushed(120)
    got = feed(tr, seg((123, 26.3), end=120 + 3 * 3600))
    reminds = ops(got, "remind")
    assert len(reminds) == 1
    t, a = reminds[0]
    assert t == 3720
    assert (a.kind, a.level, a.push, a.value) == ("env_temp_high", "caution", True, 26.3)
    assert tr.active.reminded is True


def test_no_reminder_while_recovering():
    # 3590초(푸시 기준)에 25℃ → 회복 대기 중엔 1시간 지나도 remind 없음 (D15)
    tr = ElementTracker("temp")
    feed(tr, seg((0, 26.3), end=123))
    tr.mark_pushed(120)
    got = feed(tr, seg((123, 26.3), (120 + 3590, 25.0), end=120 + 3590 + 299))
    assert ops(got, "remind") == []
    assert got == []


def test_no_reminder_without_mark_pushed():
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 26.3), end=3 * 3600))
    assert ops(got, "remind") == []


# ---------- 공백·센서 오류 (D2) ----------

def test_gap_resets_timers():
    # 26.3 100초 → 61초 공백 → 26.3: 공백 뒤 100초째 없음, 120초째 open
    tr = ElementTracker("temp")
    samples = seg((0, 26.3), (102, SKIP), (160, 26.3), end=400)
    assert samples[33][0] == 99 and samples[34][0] == 160
    got = feed(tr, samples)
    assert not [t for t, _ in got if t < 280]
    assert [(t, a.op, a.at_mono) for t, a in got] == [(280, "open", 160)]


def test_gap_exactly_60_keeps_timers():
    tr = ElementTracker("temp")
    samples = seg((0, 26.3), (102, SKIP), (159, 26.3), end=300)
    got = feed(tr, samples)
    assert [(t, a.op, a.at_mono) for t, a in got] == [(159, "open", 0)]


@pytest.mark.parametrize("bad", [None, math.nan])
def test_none_or_nan_resets_timers(bad):
    tr = ElementTracker("temp")
    samples = seg((0, 26.3), (63, bad), (66, 26.3), end=300)
    got = feed(tr, samples)
    assert [(t, a.op, a.at_mono) for t, a in got] == [(186, "open", 66)]


# ---------- 습도 ----------

def test_humid_over_needs_1h_continuous():
    tr = ElementTracker("humid")
    assert feed(tr, seg((0, 85.0), end=3599)) == []
    assert feed(tr, [(3599, 85.0)]) == []
    got = feed(tr, [(3600, 85.0)])
    assert [(a.op, a.kind, a.level, a.push) for _, a in got] == [
        ("open", "env_humid_high", "caution", True)]
    assert got[0][1].threshold == 80.0
    # 1800초에 80.0 한 건 → 그 뒤로 다시 3600초
    tr = ElementTracker("humid")
    got = feed(tr, seg((0, 85.0), (1800, 80.0), (1803, 85.0), end=6000))
    assert [(t, a.op, a.at_mono) for t, a in got] == [(5403, "open", 1803)]


def test_humid_dry_2min_and_recovery():
    tr = ElementTracker("humid")
    got = feed(tr, seg((0, 39.0), (150, 45.0), end=600))
    assert [(t, a.op, a.kind) for t, a in got] == [
        (120, "open", "env_humid_low"), (450, "close", "env_humid_low")]
    assert got[1][1].at_mono == 150


# ---------- 반대쪽 전환 (D5) ----------

def test_opposite_side_switch():
    tr = ElementTracker("temp")
    _open_high_caution(tr, until=210)
    got = feed(tr, seg((210, 17.0), end=400))
    assert [(t, a.op, a.kind, a.level, a.push) for t, a in got] == [
        (330, "close", "env_temp_high", "caution", False),
        (330, "open", "env_temp_low", "caution", True),
    ]
    assert got[0][1].closes_alert_id == "a1"
    assert got[0][1].at_mono == got[1][1].at_mono == 210


# ---------- peak ----------

def test_peak_includes_entry_window_and_flush():
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 26.3), (60, 27.8), (63, 26.5), end=300))
    assert [(t, a.op, a.peak) for t, a in got] == [(120, "open", 27.8)]
    got = feed(tr, seg((300, 28.1), (303, 26.5), end=700))
    peaks = ops(got, "peak")
    assert [(t, a.peak) for t, a in peaks] == [(600, 28.1)]
    assert tr.active.peak == 28.1


# ---------- 복원 ----------

def test_restore_keeps_event():
    tr = ElementTracker("temp")
    now = 10_000.0
    tr.restore(
        ActiveEvent(
            kind="env_temp_high", level="caution",
            started_wall=datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc),
            pushed_mono=now - 3500, reminded=False, silent=False, peak=26.5,
            alert_id="a9",
        ),
        now,
    )
    got = feed(tr, seg((now, 26.3), end=now + 300))
    assert ops(got, "open") == []
    reminds = ops(got, "remind")
    assert len(reminds) == 1
    assert reminds[0][0] >= now + 100


def test_rollback_open_reopens_next_sample():
    # open INSERT 실패 → rollback_open → 타이머 유지라 다음 샘플에서 다시 open
    tr = ElementTracker("temp")
    got = feed(tr, seg((0, 26.3), end=123))
    assert [a.op for _, a in got] == ["open"]
    tr.rollback_open()
    assert tr.active is None
    got = feed(tr, [(123, 26.3)])
    assert [(a.op, a.at_mono) for _, a in got] == [("open", 0)]


# ---------- 설정 ----------

def test_sanitize_thresholds():
    th = Thresholds.sanitize({
        "target_temp_min": 10, "target_temp_max": 30,
        "target_humid_min": 85, "target_humid_max": 85,
    })
    assert (th.temp_min, th.temp_max) == (15.0, 29.0)
    assert (th.humid_min, th.humid_max) == (40.0, 80.0)
    # 행 없음·NULL → 디폴트
    assert Thresholds.sanitize({}) == Thresholds.default()
    assert Thresholds.sanitize({"target_temp_max": None, "target_humid_min": 50}) == Thresholds(
        18.0, 27.0, 50.0, 80.0)
    # MIN ≥ MAX → 그 요소만 디폴트
    th = Thresholds.sanitize({"target_temp_min": 25, "target_temp_max": 24, "target_humid_min": 45})
    assert (th.temp_min, th.temp_max, th.humid_min) == (18.0, 27.0, 45.0)


# ---------- 타이틀 ----------

def test_titles():
    assert title("env_temp_high", "caution", 26.04, 26.0) == "[고온 주의] 적정 온도보다 +0.1℃"
    assert title("env_humid_high", "caution", 80.4, 80.0) == "[과습 주의] 적정 습도보다 +1%p"
    assert title("env_humid_low", "caution", 33.4, 40.0) == "[건조 주의] 적정 습도보다 -7%p"
    assert title("env_temp_low", "danger", 14.2, None) == (
        "[저온 경고] 온도가 너무 낮아 개체가 위험할 수 있어요")
    assert title("env_temp_high", "danger", 29.4, 26.0) == (
        "[고온 경고] 온도가 너무 높아 개체가 위험할 수 있어요")
    # 부동소수 오차로 0.3 이 0.4 로 올라가지 않음(유저 체험 시뮬레이션 1)
    assert title("env_temp_high", "caution", 26.3, 26.0) == "[고온 주의] 적정 온도보다 +0.3℃"
    assert title("env_temp_low", "caution", 17.5, 18.0) == "[저온 주의] 적정 온도보다 -0.5℃"


def test_default_temp_max_27() -> None:
    # 2026-11-04 앱 요청: 저장값 없는 기기의 적정 온도 상한 26 → 27℃ (하한 18 그대로)
    assert (Thresholds.default().temp_min, Thresholds.default().temp_max) == (18.0, 27.0)
    assert Thresholds.sanitize({"target_temp_min": 20.0}).temp_max == 27.0
    assert Thresholds.sanitize({"target_temp_max": 26.0}).temp_max == 26.0     # 사용자 저장값은 그대로
