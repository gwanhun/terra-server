"""daily_analysis — v4 일일 분석 카드(온도·습도) 순수 함수 단위 테스트.

계획: specs/stage-p-daily-analysis-v4.md Task 1 (결정 P4~P8, P11, P12, 문구 원문 고정).
입력 행은 Stage N `backend/alerts.py` 가 실제로 남기는 모양을 흉내 낸다:
`triggered_at`/`resolved_at`(ISO UTC 문자열), `context`(rule·level·value·threshold·diff·peak_value·
peak_diff·chain_root·deescalated_from·push_suppressed).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.daily_analysis import (
    KST,
    _daynight_avgs,
    build_humid_card,
    build_temp_card,
    report_window,
)
from backend.env_rules import ElementTracker, Thresholds, display_diff

UTC = timezone.utc
W = report_window(date(2026, 11, 2))          # 11/2 08:00 ~ 11/3 08:00 KST


def kst(day: int, hh: int, mm: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=KST)


def iso(dt: datetime | None) -> str | None:
    """DB(timestamptz) 가 돌려주는 모양 — UTC ISO 문자열."""
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def ev(id_: str, kind: str, level: str, start: datetime, end: datetime | None, **ctx) -> dict:
    """alerts v4 행 하나."""
    return {
        "id": id_,
        "kind": kind,
        "severity": "critical" if level == "danger" else "warning",
        "triggered_at": iso(start),
        "resolved_at": iso(end),
        "context": {"rule": "v4", "level": level, **ctx},
    }


def bucket(at: datetime, avg: float | None, count: int | None, h_avg: float | None = 50.0) -> dict:
    return {
        "bucket": iso(at),
        "t_a_avg": avg, "t_a_count": count,
        "h_a_avg": h_avg, "h_a_count": count,
    }


def day_night_buckets(day: float, night: float, count: int = 600) -> list[dict]:
    """11/2 08:00~11/3 08:00 의 30분 버킷 48개. 08~20시 = day, 나머지 = night. 합 48×count."""
    out = []
    t = kst(2, 8)
    while t < kst(3, 8):
        out.append(bucket(t, day if 8 <= t.hour < 20 else night, count))
        t += timedelta(minutes=30)
    return out


# ---------- 구간 ----------

def test_window_utc_bounds():
    start, end = report_window(date(2026, 11, 2))
    assert start == datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 23, 0, tzinfo=UTC)


# ---------- 온도 이벤트 ----------

def test_temp_priority_and_order():
    events = [
        ev("d1", "env_temp_high", "danger", kst(2, 14, 30), kst(2, 15, 50), peak_value=29.6, threshold=26.0),
        ev("c1", "env_temp_high", "caution", kst(2, 14), kst(2, 14, 30),
           peak_value=27.8, peak_diff=1.8, threshold=26.0),
    ]
    card = build_temp_card(events, [], W)
    assert card is not None
    assert card.title == "위험한 온도가 기록되었어요"
    assert card.status == "danger"
    assert card.lines == [
        "14:00~14:30 적정 온도보다 최대 +1.8℃ 이탈",
        "14:30~15:50 위험 온도 29.6℃ 감지",
    ]


def test_temp_caution_titles_and_low_side():
    high = build_temp_card([ev("c1", "env_temp_high", "caution", kst(2, 14), kst(2, 15),
                               peak_diff=0.4, threshold=26.0)], [], W)
    assert (high.title, high.status) == ("높은 온도가 기록됐어요", "caution")
    both = build_temp_card([
        ev("c2", "env_temp_low", "caution", kst(3, 3), kst(3, 4), peak_value=16.75, threshold=18.0),
        ev("c1", "env_temp_high", "caution", kst(2, 14), kst(2, 15), peak_diff=0.4, threshold=26.0),
    ], [], W)
    assert both.title == "높은 온도가 기록됐어요"       # 고온 주의(3) > 저온 주의(4)
    assert both.lines == [
        "14:00~15:00 적정 온도보다 최대 +0.4℃ 이탈",
        "03:00~04:00 적정 온도보다 최대 -1.3℃ 이탈",     # |16.75-18| = 1.25 → 올림 1.3 (D10 과 같은 값)
    ]
    low = build_temp_card([ev("c2", "env_temp_low", "caution", kst(3, 3), kst(3, 4),
                              peak_diff=1.3, threshold=18.0)], [], W)
    assert low.title == "낮은 온도가 기록됐어요"
    low_danger = build_temp_card([ev("d2", "env_temp_low", "danger", kst(3, 3), kst(3, 4),
                                     peak_value=14.2, threshold=18.0)], [], W)
    assert low_danger.title == "위험한 온도가 기록되었어요"
    assert low_danger.lines == ["03:00~04:00 위험 온도 14.2℃ 감지"]


def test_chain_merged_line():
    events = [
        ev("d1", "env_temp_high", "danger", kst(2, 14, 30), kst(2, 15, 50), peak_value=29.6, threshold=26.0),
        ev("s1", "env_temp_high", "caution", kst(2, 15, 50), kst(2, 16, 20),
           peak_value=27.5, peak_diff=1.5, threshold=26.0, deescalated_from="d1", chain_root="d1"),
    ]
    card = build_temp_card(events, [], W)
    assert card.lines == ["14:30~16:20 위험 온도 29.6℃ 감지"]
    assert card.status == "danger"
    assert card.data["events"][0]["ids"] == ["d1", "s1"]


def test_chain_takes_most_extreme_peak_and_open_end():
    # 경고 → 무음 주의(D3) → 무음 재경고(D14, peak 30.2, 진행 중) = 한 줄, 끝 시각 없음
    events = [
        ev("d1", "env_temp_high", "danger", kst(2, 14, 30), kst(2, 15, 50), peak_value=29.6, threshold=26.0),
        ev("s1", "env_temp_high", "caution", kst(2, 15, 50), kst(2, 16, 5),
           peak_value=28.9, threshold=26.0, deescalated_from="d1", chain_root="d1"),
        ev("d2", "env_temp_high", "danger", kst(2, 16, 5), None,
           peak_value=30.2, threshold=26.0, push_suppressed=True, chain_root="d1"),
    ]
    card = build_temp_card(events, [], W)
    assert card.lines == ["14:30~ 위험 온도 30.2℃ 감지"]


def test_ongoing_event_no_end():
    card = build_temp_card([ev("d1", "env_temp_high", "danger", kst(2, 14), None,
                               peak_value=29.6, threshold=26.0)], [], W)
    assert card.lines == ["14:00~ 위험 온도 29.6℃ 감지"]


def test_event_overlapping_both_days():
    e = ev("c1", "env_temp_high", "caution", kst(2, 7, 50), kst(2, 8, 20), peak_diff=0.5, threshold=26.0)
    prev = build_temp_card([e], [], report_window(date(2026, 11, 1)))
    cur = build_temp_card([e], [], W)
    assert prev.lines == ["07:50~11/2 08:20 적정 온도보다 최대 +0.5℃ 이탈"]
    assert cur.lines == ["11/2 07:50~08:20 적정 온도보다 최대 +0.5℃ 이탈"]


def test_event_outside_window_excluded():
    # 구간 끝(11/3 08:00) 이후 시작, 구간 시작(11/2 08:00) 전에 종료 → 둘 다 제외 (P4)
    events = [
        ev("a", "env_temp_high", "caution", kst(3, 8), None, peak_diff=0.5, threshold=26.0),
        ev("b", "env_temp_high", "caution", kst(2, 6), kst(2, 8), peak_diff=0.5, threshold=26.0),
    ]
    assert build_temp_card(events, [], W) is None


def test_cross_day_time_format():
    w1 = report_window(date(2026, 11, 1))
    a = ev("c1", "env_temp_high", "caution", kst(1, 22), kst(2, 8, 20), peak_diff=0.5, threshold=26.0)
    b = ev("c1", "env_temp_high", "caution", kst(1, 22), kst(2, 3), peak_diff=0.5, threshold=26.0)
    assert build_temp_card([a], [], w1).lines == ["22:00~11/2 08:20 적정 온도보다 최대 +0.5℃ 이탈"]
    assert build_temp_card([b], [], w1).lines == ["22:00~03:00 적정 온도보다 최대 +0.5℃ 이탈"]


def test_peak_fallbacks():
    # peak_diff 없음 → |peak_value − threshold| (D10 올림과 같은 값)
    a = build_temp_card([ev("c1", "env_temp_high", "caution", kst(2, 14), None,
                            peak_value=27.34, threshold=26.0, value=26.3)], [], W)
    assert a.lines == ["14:00~ 적정 온도보다 최대 +1.4℃ 이탈"]
    # peak_value 도 없음 → context.value
    b = build_temp_card([ev("c1", "env_temp_high", "caution", kst(2, 14), None,
                            threshold=26.0, value=26.3)], [], W)
    assert b.lines == ["14:00~ 적정 온도보다 최대 +0.3℃ 이탈"]
    d = build_temp_card([ev("d1", "env_temp_high", "danger", kst(2, 14), None,
                            threshold=26.0, value=29.4)], [], W)
    assert d.lines == ["14:00~ 위험 온도 29.4℃ 감지"]
    # 값이 하나도 없으면 줄을 빼고(타이틀은 유지) "None" 을 만들지 않는다
    e = build_temp_card([ev("d1", "env_temp_high", "danger", kst(2, 14), None)], [], W)
    assert e.title == "위험한 온도가 기록되었어요" and e.lines == []
    for card in (a, b, d, e):
        assert "None" not in repr(card.to_json()["lines"]) and "nan" not in repr(card.lines)


# ---------- 주야간 ----------

def test_daynight_buckets():
    card = build_temp_card([], day_night_buckets(25.1, 21.8), W)
    assert card is not None
    assert card.title == "주간과 야간 모두 적정 온도를 유지했어요"
    assert card.lines == ["안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요"]
    assert card.status == "normal" and card.code == "TEMP_NORMAL"
    assert card.data["diff_daynight"] == 3.3
    assert (card.data["day_avg"], card.data["night_avg"]) == (25.1, 21.8)


def test_daynight_weighted_by_count():
    buckets = [bucket(kst(2, 10), 26.0, 600), bucket(kst(2, 10, 30), 20.0, 60), bucket(kst(2, 22), 21.0, 600)]
    day, night = _daynight_avgs(buckets, W)
    assert day == pytest.approx(25.4545, abs=0.0001)   # (26×600 + 20×60) / 660
    assert night == pytest.approx(21.0)


def test_daynight_excludes_buckets_outside_window():
    buckets = day_night_buckets(25.0, 22.0) + [bucket(kst(3, 8), 40.0, 600), bucket(kst(2, 7, 30), 40.0, 600)]
    assert _daynight_avgs(buckets, W) == (pytest.approx(25.0), pytest.approx(22.0))


@pytest.mark.parametrize("day,title,line", [
    (21.449, "주간과 야간 온도가 거의 일정했어요", "건강한 생체 리듬을 위해 주야 온도 차이 3~5℃를 권장해요"),
    (21.45, "주간과 야간 모두 적정 온도를 유지했어요", "안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요"),
    (26.04, "주간과 야간 모두 적정 온도를 유지했어요", "안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요"),
    (26.05, "주간과 야간의 온도차가 너무 커요",
     "주간·야간 평균 편차 6.1℃. 건강한 생체 리듬을 위해 주야 온도 차이 3~5℃를 권장해요"),
], ids=["1.449", "1.45", "6.04", "6.05"])
def test_daynight_display_based_thresholds(day, title, line):
    # 야간 20.0 고정 → 차이 1.449 / 1.45(표시 1.5) / 6.04(표시 6.0) / 6.05(표시 6.1)
    card = build_temp_card([], day_night_buckets(day, 20.0), W)
    assert card.title == title
    assert card.lines == [line]
    assert card.status == ("normal" if title.startswith("주간과 야간 모두") else "caution")


def test_no_night_samples_no_card():
    buckets = [b for b in day_night_buckets(25.0, 22.0, count=960) if 8 <= datetime.fromisoformat(
        b["bucket"]).astimezone(KST).hour < 20]                      # 주간 24개 × 960 = 23,040 (유효)
    assert build_temp_card([], buckets, W) is None
    zero_night = [dict(b, t_a_count=0) if datetime.fromisoformat(b["bucket"]).astimezone(KST).hour >= 20
                  or datetime.fromisoformat(b["bucket"]).astimezone(KST).hour < 8 else dict(b, t_a_count=960)
                  for b in day_night_buckets(25.0, 22.0)]
    assert build_temp_card([], zero_night, W) is None


def test_events_shown_without_buckets():
    card = build_temp_card([ev("d1", "env_temp_high", "danger", kst(2, 14), kst(2, 15),
                               peak_value=29.6, threshold=26.0)], [], W)
    assert card is not None and card.title == "위험한 온도가 기록되었어요"
    assert card.data["day_avg"] is None and card.data["diff_daynight"] is None


def test_events_card_carries_daynight_data():
    card = build_temp_card([ev("c1", "env_temp_high", "caution", kst(2, 14), kst(2, 15),
                               peak_diff=0.4, threshold=26.0)], day_night_buckets(25.1, 21.8), W)
    assert card.title == "높은 온도가 기록됐어요"
    assert card.data["diff_daynight"] == 3.3 and card.data["valid_day"] is True


def test_invalid_day_no_daynight_card():
    buckets = day_night_buckets(25.1, 21.8, count=480)    # 48 × 480 = 23,040
    buckets[0]["t_a_count"] = 479                          # 23,039
    assert build_temp_card([], buckets, W) is None
    buckets[0]["t_a_count"] = 480                          # 23,040 = 28,800 × 0.8
    card = build_temp_card([], buckets, W)
    assert card is not None and card.title == "주간과 야간 모두 적정 온도를 유지했어요"


# ---------- 습도 ----------

def test_humid_priority_dry_over():
    events = [
        ev("h1", "env_humid_high", "caution", kst(2, 10), kst(2, 12), peak_value=88.0, threshold=80.0),
        ev("h2", "env_humid_low", "caution", kst(2, 22), None, peak_value=30.2, threshold=40.0),
    ]
    card = build_humid_card(events, [], W)
    assert card.title == "건조를 주의해 주세요"
    assert card.status == "caution" and card.element == "humid"
    assert card.lines == [
        "10:00~12:00 적정 습도보다 최대 +8% 이탈",
        "22:00~ 적정 습도보다 최대 -10% 이탈",            # |30.2-40| = 9.8 → 올림 10
    ]
    over = build_humid_card(events[:1], [], W)
    assert over.title == "과습을 주의해 주세요"


def test_humid_vent_delay_line():
    vent = {
        "id": "v1", "kind": "env_vent_delay", "severity": "warning",
        "triggered_at": iso(kst(2, 16)), "resolved_at": iso(kst(2, 18)),
        "context": {"rule": "v4", "level": "caution", "cycle_started_at": iso(kst(2, 10))},
    }
    card = build_humid_card([vent], [], W)
    assert card.title == "건습 사이클이 건강하지 않아요"
    assert (card.code, card.status) == ("HUM_DELAY", "danger")   # 주간 HUM_DELAY(경고)와 같은 어휘
    assert card.lines == ["10:00에 분무 후 사육장이 충분히 마르지 않음"]
    no_start = dict(vent, context={"rule": "v4", "level": "caution"})
    card2 = build_humid_card([no_start], [], W)
    assert card2.title == "건습 사이클이 건강하지 않아요" and card2.lines == []


def test_humid_no_event_no_cycles_none():
    assert build_humid_card([], day_night_buckets(25.0, 22.0), W, cycles=None) is None


def test_humid_cycles_healthy_and_none():
    cycles = [
        {"started_at": iso(kst(2, 9)), "dry_reached_at": iso(kst(2, 12)), "status": "reached_in_time"},
        {"started_at": iso(kst(2, 15)), "dry_reached_at": iso(kst(2, 23)), "status": "reached_late"},
        {"started_at": iso(kst(3, 2)), "dry_reached_at": None, "status": "open"},
    ]
    card = build_humid_card([], [], W, cycles=cycles)
    assert card.title == "건습 사이클이 건강하게 유지됐어요"
    assert card.lines == ["습기 3번 , 건기 2번이 기록됐어요"]
    assert card.status == "normal"
    none = build_humid_card([], [], W, cycles=[])
    assert none.title == "습기가 감지되지 않았어요"
    assert none.lines == ["분무를 잊지 않으셨는지 확인해 주세요"]
    assert none.status == "caution"


# ---------- 직렬화 ----------

def test_card_json_has_element_code_status():
    cards = [
        build_temp_card([ev("d1", "env_temp_high", "danger", kst(2, 14), None, peak_value=29.6)], [], W),
        build_temp_card([], day_night_buckets(25.1, 21.8), W),
        build_humid_card([], [], W, cycles=[]),
    ]
    for c in cards:
        j = c.to_json()
        assert set(j) == {"element", "code", "status", "title", "lines", "data"}
        assert j["status"] in {"danger", "caution", "normal"}
        assert j["element"] in {"temp", "humid"}
        assert isinstance(j["code"], str)


# ---------- Stage N 실제 행 ----------

def _replay_rows(samples: list[tuple[float, float]], base: datetime) -> list[dict]:
    """ElementTracker 를 돌리고 Action 을 alerts.py 처럼 행으로 남긴다(_insert_open/_write_close/peak)."""
    th = Thresholds.default()
    tr = ElementTracker("temp")
    rows: dict[str, dict] = {}
    seq = 0

    def wall(at: float) -> str:
        return iso(base + timedelta(seconds=at))

    for t, v in samples:
        prev = tr.active
        for a in tr.step(t, v, th):
            if a.op == "close":
                r = rows[a.closes_alert_id]
                r["resolved_at"] = wall(a.at_mono)
                r["context"]["peak_value"] = a.peak
                r["context"]["peak_diff"] = display_diff(a.kind, a.peak, r["context"]["threshold"])
            elif a.op == "open":
                seq += 1
                aid = f"a{seq}"
                ctx = {"rule": "v4", "level": a.level, "value": a.value, "threshold": a.threshold,
                       "diff": display_diff(a.kind, a.value, a.threshold), "peak_value": a.peak}
                if tr.active.silent and prev is not None:
                    ctx["deescalated_from"] = prev.alert_id
                    ctx["chain_root"] = rows[prev.alert_id]["context"].get("chain_root") or prev.alert_id
                elif a.level == "danger" and not a.push and prev is not None:
                    ctx["push_suppressed"] = True
                    ctx["chain_root"] = rows[prev.alert_id]["context"].get("chain_root") or prev.alert_id
                rows[aid] = {"id": aid, "kind": a.kind,
                             "severity": "critical" if a.level == "danger" else "warning",
                             "triggered_at": wall(a.at_mono), "resolved_at": None, "context": ctx}
                tr.active.alert_id = aid
                if a.push:
                    tr.mark_pushed(t)
            elif a.op == "peak":
                rows[tr.active.alert_id]["context"]["peak_value"] = a.peak
    return list(rows.values())


def test_card_from_env_rules_rows():
    # Stage N 유저 체험 시나리오(MAX 26) — 13:50 기준 초. 3초 간격.
    plan = [
        (0, 24.0),       # 13:50
        (600, 26.3),     # 14:00 고온 주의 시작
        (900, 26.0),     # 14:05 잠깐 적정(5분 미달)
        (1020, 26.2),    # 14:07
        (1200, 27.8),    # 14:10 주의 peak
        (1500, 26.5),    # 14:15
        (2400, 29.4),    # 14:30 경고
        (6000, 29.6),    # 15:30 경고 peak
        (7200, 27.5),    # 15:50 팬 → 무음 주의
        (9000, 25.5),    # 16:20 적정
    ]
    samples = []
    for (start, v), stop in zip(plan, [p[0] for p in plan[1:]] + [9600]):
        samples += [(float(t), v) for t in range(start, stop, 3)]
    rows = _replay_rows(samples, kst(2, 13, 50))

    kinds = [(r["context"]["level"], r["triggered_at"], r["resolved_at"], "chain_root" in r["context"])
             for r in rows]
    assert kinds == [
        ("caution", iso(kst(2, 14)), iso(kst(2, 14, 30)), False),
        ("danger", iso(kst(2, 14, 30)), iso(kst(2, 15, 50)), False),
        ("caution", iso(kst(2, 15, 50)), iso(kst(2, 16, 20)), True),
    ]
    card = build_temp_card(rows, [], W)
    assert card.title == "위험한 온도가 기록되었어요"
    assert card.lines == [
        "14:00~14:30 적정 온도보다 최대 +1.8℃ 이탈",
        "14:30~16:20 위험 온도 29.6℃ 감지",
    ]
