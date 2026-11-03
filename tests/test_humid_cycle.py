"""humid_cycle — Stage O 습기 사이클 순수 상태 기계 단위 테스트.

계획: specs/stage-o-humid-cycle-v4.md Task 1 (결정 O-a~O-h, O2~O5).
샘플은 3초 간격, 키프레임 사이는 직선 보간. 6·15시간 경계는 사이클을 직접 만들어 복원한 뒤 본다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.humid_cycle import (
    MAX_CAP_PCT,
    CycleOp,
    CycleState,
    HumidCycleTracker,
    humid_cycles_enabled,
    humid_cycles_since,
    max_humid,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)   # 20:00 KST


def at(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


def run(tr: HumidCycleTracker, points: list[tuple[float, float]], step: int = 3) -> list[CycleOp]:
    """키프레임 [(초, 습도)] 를 3초 간격 샘플로 흘려 넣고 나온 op 전부."""
    ops: list[CycleOp] = []
    for (s0, h0), (s1, h1) in zip(points, points[1:]):
        t = s0
        while t < s1:
            ops += tr.step(at(t), h0 + (h1 - h0) * (t - s0) / (s1 - s0))
            t += step
    ops += tr.step(at(points[-1][0]), points[-1][1])
    return ops


def names(ops: list[CycleOp]) -> list[str]:
    return [o.op for o in ops]


def of(ops: list[CycleOp], op: str) -> list[CycleOp]:
    return [o for o in ops if o.op == op]


def cycle(**kw) -> CycleState:
    """복원용 사이클. 기본: T0 시작, MAX 70 확정(건기 목표 58), 미도달."""
    base = dict(
        cycle_id="c1", started_at=T0, start_humid=50.0, detected_at=at(60), peak=70.0,
        max_until=at(240), dry_target=58.0, dry_reached_at=None, status="open",
        offline_sec=0.0, merged_count=0,
        vent_delay_alert_id=None, vent_delay_fired=False, vent_delay_closed=False,
        mist_gap_alert_id=None, mist_gap_fired=False, mist_gap_closed=False,
    )
    base.update(kw)
    return CycleState(**base)


def until_open(tr: HumidCycleTracker, points: list[tuple[float, float]]) -> list[CycleOp]:
    ops = run(tr, points)
    assert len(of(ops, "open")) == 1
    return ops


# ---------- 스위치 ----------

def test_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUMID_CYCLES_ENABLED", raising=False)
    monkeypatch.delenv("HUMID_CYCLES_SINCE", raising=False)
    assert humid_cycles_enabled() is False and humid_cycles_since() is None
    monkeypatch.setenv("HUMID_CYCLES_ENABLED", "true")
    monkeypatch.setenv("HUMID_CYCLES_SINCE", "2026-11-04T09:00:00+09:00")
    assert humid_cycles_enabled() is True
    assert humid_cycles_since() == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    monkeypatch.setenv("HUMID_CYCLES_SINCE", "어제")
    assert humid_cycles_since() is None


def test_max_humid_cap() -> None:
    assert MAX_CAP_PCT == 85.0
    assert max_humid(90.0) == 85.0 and max_humid(72.25) == 72.3


# ---------- 감지 (O-a) ----------

def test_rise_detects_wet_event() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50), (720, 65), (900, 65)])
    assert names(ops) == ["open", "max_fixed"]           # MAX 창(감지 + 3분)도 900초 안에 끝남
    o = ops[0]
    assert at(600) <= o.at <= at(620)              # 상승 직전 마지막 50% 샘플(30초 중앙값)
    assert o.cycle.start_humid == 50.0 and o.cycle.started_at == o.at
    assert tr.cycle is o.cycle and tr.cycle.status == "open"


@pytest.mark.parametrize("lo, hi", [(50, 59), (52, 61)])
def test_rise_below_threshold_not_wet(lo: float, hi: float) -> None:
    assert run(HumidCycleTracker(), [(0, lo), (600, lo), (660, hi), (900, hi)]) == []


def test_high_start_needs_5pp() -> None:
    assert len(of(run(HumidCycleTracker(), [(0, 64), (600, 64), (660, 69.5), (900, 69.5)]), "open")) == 1
    assert run(HumidCycleTracker(), [(0, 64), (600, 64), (660, 68.5), (900, 68.5)]) == []


def test_wet_needs_60_after_rise() -> None:
    assert run(HumidCycleTracker(), [(0, 40), (600, 40), (660, 55), (900, 55)]) == []


def test_slow_rise_not_wet() -> None:
    assert run(HumidCycleTracker(), [(0, 50), (600, 50), (2400, 70), (3000, 70)]) == []


def test_spike_smoothed_out() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50)])
    ops += tr.step(at(603), 98.0)
    ops += run(tr, [(606, 50), (900, 50)])
    assert ops == []


def test_single_rise_plateau_no_merge() -> None:
    ops = run(HumidCycleTracker(), [(0, 50), (600, 50), (720, 70), (1320, 70)])
    assert len(of(ops, "open")) == 1 and of(ops, "merge") == []


def test_none_sample_ignored() -> None:
    tr = HumidCycleTracker(cycle=cycle(), last_sample_at=at(997))
    assert tr.step(at(1000), 65.0) == []
    assert tr.step(at(1003), None) == []
    assert tr.cycle.offline_sec == 0.0


# ---------- MAX (O-c·O-d) ----------

def test_max_window_from_detection_and_cap() -> None:
    tr = HumidCycleTracker()
    until_open(tr, [(0, 50), (600, 50), (660, 70), (700, 70)])
    det = (tr.cycle.detected_at - T0).total_seconds()
    assert tr.cycle.max_until == tr.cycle.detected_at + timedelta(seconds=180)
    ops = run(tr, [(det + 100, 70), (det + 110, 90), (det + 400, 90)])
    fixed = of(ops, "max_fixed")
    assert len(fixed) == 1 and fixed[0].at > tr.cycle.max_until
    assert tr.cycle.peak == 90.0 and tr.cycle.dry_target == 73.0     # 85 − 12


def test_dry_not_checked_before_max_fixed() -> None:
    tr = HumidCycleTracker()
    until_open(tr, [(0, 50), (600, 50), (660, 70), (700, 70)])
    det = (tr.cycle.detected_at - T0).total_seconds()
    ops = run(tr, [(det + 30, 70), (det + 60, 50), (det + 170, 50)])  # 창 안에서 목표 아래
    assert of(ops, "dry_reached") == [] and tr.cycle.dry_target is None


# ---------- 병합 (O-g) ----------

def test_merge_within_20min_refixes() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50), (660, 70), (900, 70), (1200, 64), (1320, 64)])
    assert len(of(ops, "max_fixed")) == 1 and tr.cycle.dry_target == 58.0
    ops += run(tr, [(1323, 64), (1350, 74), (1700, 74)])
    assert len(of(ops, "open")) == 1 and len(of(ops, "merge")) == 1
    assert len(of(ops, "max_fixed")) == 2 and tr.cycle.dry_target == 62.0
    assert of(ops, "dry_reached") == [] and tr.cycle.merged_count == 1


def test_new_event_after_20min_not_reached() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50), (660, 70), (1200, 70), (1500, 64), (2100, 64), (2130, 74), (2400, 74)])
    opens = of(ops, "open")
    assert len(opens) == 2
    i = names(ops).index("open", names(ops).index("open") + 1)
    assert names(ops)[i:i + 2] == ["open", "not_reached"]
    prev = ops[i + 1].cycle
    assert prev.status == "not_reached" and prev is not tr.cycle
    assert ops[i + 1].at == opens[1].cycle.started_at


def test_new_event_after_dry_within_20min() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50), (660, 70), (900, 70), (1000, 55), (1300, 55), (1330, 70), (1500, 70)])
    assert len(of(ops, "dry_reached")) == 1
    assert len(of(ops, "open")) == 2 and of(ops, "merge") == [] and of(ops, "not_reached") == []


# ---------- 건기 (O2·O3) ----------

def test_dry_reached_in_time() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50), (660, 70), (1200, 70), (18000, 57), (18300, 57), (18600, 65)])
    dry = of(ops, "dry_reached")
    assert len(dry) == 1 and tr.cycle.status == "reached_in_time"
    assert tr.cycle.dry_reached_at == dry[0].at
    assert of(ops, "vent_delay") == []


def test_dry_and_6h_same_sample_dry_wins() -> None:
    tr = HumidCycleTracker(cycle=cycle(), last_sample_at=at(21597))
    ops = tr.step(at(21600), 58.0)
    assert names(ops) == ["dry_reached"] and tr.cycle.status == "reached_in_time"


def test_vent_delay_at_6h() -> None:
    tr = HumidCycleTracker(cycle=cycle(), last_sample_at=at(20997))
    ops = run(tr, [(21000, 65), (21606, 65)])
    assert names(ops) == ["vent_delay"] and ops[0].at == at(21600)
    assert tr.cycle.vent_delay_fired is True


def test_reached_late_closes_vent_delay() -> None:
    tr = HumidCycleTracker(cycle=cycle(vent_delay_fired=True), last_sample_at=at(25197))
    ops = run(tr, [(25200, 65), (25260, 55), (25400, 55)])
    assert names(ops) == ["dry_reached", "vent_delay_close"]
    assert tr.cycle.status == "reached_late"
    assert ops[1].reason == "dry" and ops[1].at == ops[0].at


def test_vent_delay_stale_at_15h() -> None:
    tr = HumidCycleTracker(cycle=cycle(vent_delay_fired=True), last_sample_at=at(53997))
    ops = run(tr, [(54000, 65), (54100, 65)])
    assert names(ops) == ["vent_delay_close"] and ops[0].reason == "stale"
    assert tr.cycle.status == "open"


def test_offline_extends_vent_delay() -> None:
    tr = HumidCycleTracker(cycle=cycle(), last_sample_at=at(-3))
    ops = run(tr, [(0, 65), (7200, 65)])
    ops += run(tr, [(12600, 65), (27003, 65)])
    assert names(ops) == ["offline", "vent_delay"]
    assert tr.cycle.offline_sec == 5400.0 and ops[1].at == at(27000)


def test_gap_resets_rise_window() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50)])
    ops += run(tr, [(720, 70), (900, 70)])
    assert ops == []


def test_reconnect_spike_needs_history() -> None:
    tr = HumidCycleTracker()
    ops = run(tr, [(0, 50), (600, 50)])
    ops += tr.step(at(720), 50.0) + tr.step(at(723), 98.0) + tr.step(at(726), 98.0)
    ops += run(tr, [(729, 50), (1000, 50)])
    assert ops == []


# ---------- 분무 공백 (O-e·O5) ----------

def reached(**kw) -> CycleState:
    return cycle(status="reached_in_time", dry_reached_at=at(9000), **kw)


def test_mist_gap_after_dry_15h() -> None:
    tr = HumidCycleTracker(cycle=reached(), last_sample_at=at(52997))
    ops = run(tr, [(53000, 50), (54003, 50)])
    assert names(ops) == ["mist_gap"] and at(54000) <= ops[0].at < at(54003)   # 15시간 이후 첫 샘플
    ops = run(tr, [(54006, 50), (54600, 50), (54660, 70), (54800, 70)])
    assert names(ops)[:2] == ["open", "mist_gap_close"]
    assert ops[1].at == ops[0].at


def test_mist_gap_excludes_offline_on_closed_cycle() -> None:
    tr = HumidCycleTracker(cycle=reached(), last_sample_at=at(29997))
    ops = run(tr, [(30000, 50), (31000, 50)])
    ops += run(tr, [(34600, 50), (57603, 50)])
    assert names(ops) == ["offline", "mist_gap"] and at(57600) <= ops[1].at < at(57603)


def test_no_mist_gap_while_not_dried() -> None:
    tr = HumidCycleTracker(cycle=cycle(), last_sample_at=at(53997))
    assert "mist_gap" not in names(run(tr, [(54000, 65), (60000, 65)]))


def test_no_cycle_no_mist_gap() -> None:
    assert run(HumidCycleTracker(), [(0, 50), (72000, 50)], step=30) == []


# ---------- 복원·되돌림 (O6) ----------

def test_restore_continues() -> None:
    tr = HumidCycleTracker(cycle=cycle(started_at=at(-21300)), last_sample_at=at(-3))
    ops = run(tr, [(0, 65), (303, 65)])
    assert names(ops) == ["vent_delay"] and ops[0].at == at(300)
    tr = HumidCycleTracker(cycle=cycle(started_at=at(-21300), vent_delay_fired=True), last_sample_at=at(-3))
    assert run(tr, [(0, 65), (303, 65)]) == []


def test_snapshot_restore_reemits() -> None:
    tr = HumidCycleTracker()
    run(tr, [(0, 50), (600, 50)])
    first: CycleOp | None = None
    again: CycleOp | None = None
    t = 603
    while t < 900 and again is None:
        h = min(70.0, 50 + (t - 600) / 3)
        snap = tr.snapshot()
        ops = tr.step(at(t), h)
        if first is None and of(ops, "open"):
            first = of(ops, "open")[0]
            tr.restore_snapshot(snap)
            assert tr.cycle is None
        elif first is not None and of(ops, "open"):
            again = of(ops, "open")[0]
        t += 3
    assert first is not None and again is not None
    assert again.cycle.started_at == first.cycle.started_at
