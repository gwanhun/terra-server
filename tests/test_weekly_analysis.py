"""weekly_analysis — v4 주간 분석 카드(온도·습도)·예외 순수 함수 단위 테스트.

계획: specs/stage-q-weekly-analysis-v4.md Task 1 (결정 Q1~Q14, 문구 원문 고정).
이벤트 행은 Stage N `backend/alerts.py` 가 남기는 모양(test_daily_analysis 와 같은 흉내),
버킷은 `telemetry_30m`(t_a_avg·t_a_count·h_a_avg·h_a_count), 사이클은 Stage O `humid_cycles`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.daily_analysis import KST
from backend.weekly_analysis import (
    WeeklyResult,
    build_weekly,
    fmt_dry_time,
    missing_rate,
    valid_days,
    week_window,
)

UTC = timezone.utc
WEEK = date(2026, 11, 2)                 # 월요일. 11/2 08:00 ~ 11/9 08:00 KST
W = week_window(WEEK)
PREV_W = week_window(WEEK - timedelta(days=7))
REG = W[0]                               # 구간 시작 전부터 등록된 기기


def kst(day: int, hh: int, mm: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=KST)


def iso(dt: datetime | None) -> str | None:
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


def bucket(at: datetime, t_count: int, h_count: int | None = None, avg: float | None = 25.0) -> dict:
    return {
        "bucket": iso(at),
        "t_a_avg": avg, "t_a_count": t_count,
        "h_a_avg": 50.0, "h_a_count": t_count if h_count is None else h_count,
    }


def day_buckets(d: date, day_temp: float = 25.0, night_temp: float = 22.0,
                t_count: int = 600, h_count: int | None = None) -> list[dict]:
    """d 08:00 ~ 다음 날 08:00 KST 의 30분 버킷 48개. 08~20시 = day_temp, 나머지 = night_temp."""
    out = []
    t = datetime(d.year, d.month, d.day, 8, tzinfo=KST)
    end = t + timedelta(days=1)
    while t < end:
        out.append(bucket(t, t_count, h_count, day_temp if 8 <= t.hour < 20 else night_temp))
        t += timedelta(minutes=30)
    return out


def week_buckets(start: date = WEEK, days: range = range(7), **kw) -> list[dict]:
    out = []
    for i in days:
        out.extend(day_buckets(start + timedelta(days=i), **kw))
    return out


FULL = week_buckets()                    # 7일 유효, 주야간 차 3.0 → TEMP_NORMAL


def danger(id_: str, start: datetime, end: datetime | None, diff: float = 3.0, **ctx) -> dict:
    return ev(id_, "env_temp_high", "danger", start, end,
              value=29.5, threshold=26.0, peak_value=26.0 + diff, peak_diff=diff, **ctx)


def caution(id_: str, start: datetime, end: datetime | None, kind: str = "env_temp_high", **ctx) -> dict:
    return ev(id_, kind, "caution", start, end, value=27.0, threshold=26.0, peak_value=27.0, peak_diff=1.0, **ctx)


def humid(prefix: str, kind: str, n: int) -> list[dict]:
    """하루 하나씩 서로 다른 습도 주의 chain n 개."""
    return [ev(f"{prefix}{i}", kind, "caution", kst(2 + i, 10), kst(2 + i, 11),
               value=30.0, threshold=40.0, peak_value=28.0, peak_diff=12.0) for i in range(n)]


def cycle(start: datetime, minutes: int | None) -> dict:
    reached = start + timedelta(minutes=minutes) if minutes is not None else None
    return {"started_at": iso(start), "dry_reached_at": iso(reached),
            "status": "reached_in_time" if minutes is not None and minutes <= 360
            else "reached_late" if minutes is not None else "not_reached"}


def run(events=(), buckets=FULL, prev=None, reg=REG, cycles=None) -> WeeklyResult:
    return build_weekly(list(events), buckets, prev, W, reg, cycles)


# ---------- 구간·유효 수집일·누락률 ----------

def test_week_window_buckets():
    assert W == (datetime(2026, 11, 1, 23, 0, tzinfo=UTC), datetime(2026, 11, 8, 23, 0, tzinfo=UTC))
    # 월 07:30 버킷은 지난주(=분석 대상 주)의 마지막 날(11/8 08:00~11/9 08:00)에 속함
    assert valid_days([bucket(kst(9, 7, 30), 23040)], W, "t_a") == [date(2026, 11, 8)]
    # 월 08:00 버킷은 다음 주
    assert valid_days([bucket(kst(9, 8, 0), 23040)], W, "t_a") == []
    # 지난주 월 07:30 은 이전 주
    assert valid_days([bucket(kst(2, 7, 30), 23040)], W, "t_a") == []
    assert valid_days([bucket(kst(2, 8, 0), 23040)], W, "t_a") == [date(2026, 11, 2)]


def test_valid_days_sample_rule():
    buckets = [
        bucket(kst(3, 12), 23040, h_count=0),       # 11/3: t_a 유효, h_a 무효
        bucket(kst(4, 12), 23039, h_count=23040),   # 11/4: t_a 무효(1 모자람), h_a 유효
    ]
    assert valid_days(buckets, W, "t_a") == [date(2026, 11, 3)]
    assert valid_days(buckets, W, "h_a") == [date(2026, 11, 4)]
    assert valid_days(FULL, W, "t_a") == [WEEK + timedelta(days=i) for i in range(7)]


def test_missing_rate_from_registration():
    wed = kst(4, 8)
    after = week_buckets(start=date(2026, 11, 4), days=range(5))        # 수 08:00 이후 꽉 참
    assert missing_rate(after, W, "t_a", wed) == pytest.approx(0.0)
    half = week_buckets(start=date(2026, 11, 4), days=range(5), t_count=300)
    assert missing_rate(half, W, "t_a", wed) == pytest.approx(0.5)
    assert missing_rate(half, W, "h_a", wed) == pytest.approx(0.5)
    assert missing_rate(after, W, "t_a", kst(9, 8)) is None             # 구간 끝(=등록) → 예정 0
    assert missing_rate(after, W, "t_a", kst(10, 8)) is None            # 구간 끝 뒤 등록
    # 구간 전 등록이면 구간 시작부터 셈: 5일/7일 → 누락 2/7
    assert missing_rate(after, W, "t_a", kst(1, 8)) == pytest.approx(2 / 7)


# ---------- 예외 ----------

def test_err_no_data_per_metric():
    buckets = (week_buckets(days=range(2), h_count=600)
               + week_buckets(start=date(2026, 11, 4), days=range(3), t_count=0, h_count=600))
    r = run(humid("d", "env_humid_low", 3), buckets)
    assert r.temp_card is None
    assert r.notices == [{
        "code": "ERR_NO_DATA", "metric": "t_a",
        "title": "주간 분석을 위한 데이터를 모으고 있어요", "body": "최소 3일의 데이터가 필요해요",
    }]
    assert r.humid_card is not None and r.humid_card.code == "HUM_DRY"


def test_err_disconnect_alongside():
    # 유효 4일, 하루 23,040(80%) → 누락률 1 − 92160/201600 ≈ 0.543
    buckets = week_buckets(days=range(4), t_count=480)
    assert missing_rate(buckets, W, "t_a", REG) == pytest.approx(1 - 92160 / 201600)
    r = run(buckets=buckets)
    assert r.temp_card is not None and r.temp_card.code == "TEMP_NORMAL"
    codes = [(n["code"], n["metric"]) for n in r.notices]
    assert ("ERR_DISCONNECT", "t_a") in codes and ("ERR_DISCONNECT", "h_a") in codes
    n = next(n for n in r.notices if n["metric"] == "t_a")
    assert n["title"] == "주간 데이터 수집이 원활하지 않았어요"
    assert n["body"] == "센서 전원과 Wi-Fi 연결 상태를 확인해 주세요"
    assert run().notices == []


# ---------- 온도: 경고 ----------

def test_temp_danger_card():
    r = run([danger("d1", kst(3, 14, 10), kst(3, 14, 40), 3.2),
             danger("d2", kst(5, 14, 5), kst(5, 14, 30), 4.1)])
    c = r.temp_card
    assert c.code == "TEMP_DANGER" and c.status == "danger"
    assert c.title == "주간 중 위험 온도가 감지되었어요"
    assert c.lines == ["총 2회 위험 온도 발생. 적정 온도보다 최대 4.1℃ 이탈"]   # 2일뿐 → 패턴 줄 없음


def test_temp_danger_pattern_3days():
    r = run([danger(f"d{i}", kst(d, 14, 10), kst(d, 14, 40)) for i, d in enumerate((3, 5, 7))])
    assert r.temp_card.lines[1] == "14시~15시 사이에 온도 이탈이 집중되었어요"
    r = run([danger(f"d{i}", kst(d, 23, 10), kst(d, 23, 50)) for i, d in enumerate((2, 4, 6))])
    assert r.temp_card.lines[1] == "23시~0시 사이에 온도 이탈이 집중되었어요"
    # 13시대·14시대 둘 다 3일 → 이른 시간대
    r = run([danger(f"d{i}", kst(d, 13, 50), kst(d, 14, 20)) for i, d in enumerate((2, 4, 6))])
    assert r.temp_card.lines[1] == "13시~14시 사이에 온도 이탈이 집중되었어요"


def test_temp_danger_pattern_counts_start_hour_only():
    # 하루 가까이 이어진 경고는 걸친 시간대가 아니라 시작 시간대로만 센다(Q7, 2026-11-02 변경)
    long = [danger("a", kst(2, 5), kst(3, 1)), danger("b", kst(4, 10), kst(5, 6)),
            danger("c", kst(6, 18), kst(7, 14))]
    assert len(run(long).temp_card.lines) == 1          # 시작 05·10·18시 → 반복 없음, 주·야 섞임 → 줄 생략
    same_start = [danger(f"s{i}", kst(d, 14, 5), kst(d + 1, 9)) for i, d in enumerate((2, 4, 6))]
    assert run(same_start).temp_card.lines[1] == "14시~15시 사이에 온도 이탈이 집중되었어요"


def test_temp_danger_pattern_day_night():
    day = [danger("a", kst(2, 13), kst(2, 13, 20)), danger("b", kst(4, 15), kst(4, 15, 20)),
           danger("c", kst(6, 11), kst(6, 11, 20))]
    assert run(day).temp_card.lines[1] == "주간 시간에 온도 이탈이 집중되었어요"
    night = [danger("a", kst(2, 21), kst(2, 21, 20)), danger("b", kst(4, 2), kst(4, 2, 20)),
             danger("c", kst(6, 23), kst(6, 23, 20))]
    assert run(night).temp_card.lines[1] == "야간 시간에 온도 이탈이 집중되었어요"
    mixed = [danger("a", kst(2, 13), kst(2, 13, 20)), danger("b", kst(4, 21), kst(4, 21, 20)),
             danger("c", kst(6, 2), kst(6, 2, 20))]
    assert len(run(mixed).temp_card.lines) == 1


def test_chain_counts_once():
    chain = [
        danger("d1", kst(3, 14), kst(3, 14, 30), 3.0),
        caution("c1", kst(3, 14, 30), kst(3, 15), chain_root="d1", deescalated_from="d1"),
        danger("d2", kst(3, 15), kst(3, 15, 20), 3.5, chain_root="d1", push_suppressed=True),
    ]
    others = [caution(f"x{i}", kst(4 + i, 10), kst(4 + i, 10, 30)) for i in range(3)]
    c = run(chain + others).temp_card
    assert c.code == "TEMP_DANGER"
    assert c.lines[0] == "총 1회 위험 온도 발생. 적정 온도보다 최대 3.5℃ 이탈"
    assert c.data["n_danger"] == 1 and c.data["n_temp_caution"] == 3


def test_overlapping_prior_event_counted():
    # 지난주 일요일(11/1) 시작 → 이번 주 화요일(11/3) 종료
    c = run([danger("d1", kst(1, 20), kst(3, 10), 2.4)]).temp_card
    assert c.code == "TEMP_DANGER"
    assert c.lines[0] == "총 1회 위험 온도 발생. 적정 온도보다 최대 2.4℃ 이탈"
    # 구간 시작 전에 끝난 경고는 안 셈
    assert run([danger("d0", kst(1, 20), kst(2, 8))]).temp_card.code == "TEMP_NORMAL"


# ---------- 온도: 주의·전주 대비·주야간 ----------

def test_temp_caution_4():
    three = [caution(f"c{i}", kst(2 + i, 10), kst(2 + i, 10, 30)) for i in range(3)]
    assert run(three).temp_card.code == "TEMP_NORMAL"
    four = three + [caution("c9", kst(6, 2), kst(6, 2, 30), kind="env_temp_low")]
    c = run(four).temp_card
    assert c.code == "TEMP_CAUTION" and c.status == "caution"
    assert c.title == "적정 온도를 벗어난 경우가 많았어요"
    assert c.lines == ["총 4회 적정 온도 범위를 벗어났어요"]


def test_temp_diff_high_needs_full_weeks():
    this = week_buckets(day_temp=26.0, night_temp=24.0)                   # 주 평균 25.0, 주야간 2.0
    prev = week_buckets(start=WEEK - timedelta(days=7), day_temp=22.8, night_temp=20.8)   # 21.8
    c = run(buckets=this, prev=prev).temp_card
    assert c.code == "TEMP_DIFF_HIGH" and c.status == "caution"
    assert c.title == "지난주보다 평균 온도가 높아졌어요"
    assert c.lines == ["지난주 대비 평균 온도가 3.2℃ 상승했어요"]
    # 반대 방향
    prev_hot = week_buckets(start=WEEK - timedelta(days=7), day_temp=29.2, night_temp=27.2)  # 28.2
    c = run(buckets=this, prev=prev_hot).temp_card
    assert c.title == "지난주보다 평균 온도가 낮아졌어요"
    assert c.lines == ["지난주 대비 평균 온도가 3.2℃ 하락했어요"]
    # 2.9 → 다음 케이스
    prev_29 = week_buckets(start=WEEK - timedelta(days=7), day_temp=23.1, night_temp=21.1)  # 22.1
    assert run(buckets=this, prev=prev_29).temp_card.code == "TEMP_NORMAL"
    # 한 주가 유효 6일이면 전주 대비 생략
    prev6 = week_buckets(start=WEEK - timedelta(days=7), days=range(6), day_temp=22.8, night_temp=20.8)
    assert run(buckets=this, prev=prev6).temp_card.code == "TEMP_NORMAL"
    this6 = week_buckets(days=range(6), day_temp=26.0, night_temp=24.0)
    assert run(buckets=this6, prev=prev).temp_card.code == "TEMP_NORMAL"


def test_first_week_no_prev():
    this = week_buckets(day_temp=26.0, night_temp=24.0)
    c = run(buckets=this, prev=None).temp_card
    assert c.code == "TEMP_NORMAL"
    assert c.data["diff_prev"] is None and c.data["prev_week_avg"] is None


def test_daynight_cases():
    low = run(buckets=week_buckets(day_temp=25.0, night_temp=23.8)).temp_card
    assert low.code == "TEMP_DIFF_TOO_LOW" and low.status == "caution"
    assert low.title == "주·야간 온도차가 거의 없었어요"
    assert low.lines == ["주야간 평균 온도차가 1.2℃로 작아요. 3~5℃ 차이를 권장해요"]
    high = run(buckets=week_buckets(day_temp=28.3, night_temp=22.0)).temp_card
    assert high.code == "TEMP_DIFF_TOO_HIGH" and high.status == "caution"
    assert high.title == "주·야간 온도차가 컸어요"
    assert high.lines == ["주야간 평균 온도차가 6.3℃로 커요. 3~5℃ 내외를 권장해요"]
    ok = run(buckets=week_buckets(day_temp=25.0, night_temp=22.0)).temp_card
    assert ok.code == "TEMP_NORMAL" and ok.status == "normal"
    assert ok.title == "주·야간 온도차가 안정적으로 유지되었어요"
    assert ok.lines == ["주야간 평균 온도차가 3.0℃로 안정적이에요"]
    # 경계: 1.5 → NORMAL, 6.0 → NORMAL
    assert run(buckets=week_buckets(day_temp=25.0, night_temp=23.5)).temp_card.code == "TEMP_NORMAL"
    assert run(buckets=week_buckets(day_temp=28.0, night_temp=22.0)).temp_card.code == "TEMP_NORMAL"


def test_daynight_only_valid_days():
    buckets = week_buckets(days=range(6))                     # 6일 × 3.0
    for h in range(5):                                        # 무효일(11/8): 버킷 10개, 주야간 9.0
        buckets.append(bucket(kst(8, 9 + h), 600, avg=30.0))
        buckets.append(bucket(kst(9, 1 + h), 600, avg=21.0))
    c = run(buckets=buckets).temp_card
    assert c.code == "TEMP_NORMAL"
    assert c.lines == ["주야간 평균 온도차가 3.0℃로 안정적이에요"]


# ---------- 습도 ----------

@pytest.mark.parametrize(("dry", "over", "code", "title", "line"), [
    (2, 1, "HUM_UNSTABLE", "습도 이탈이 반복됐어요", "총 3회 습도 이탈 발생. 일정한 루틴으로 분무를 관리해 주세요"),
    (3, 2, "HUM_DRY", "사육장이 건조한 날이 많았어요", "총 3회 건조 주의가 발생. 사육장에 수분을 보충해 주세요"),
    (2, 3, "HUM_OVER", "사육장이 습한날이 많았어요", "총 3회 과습 주의가 발생. 환기 상태를 점검해 주세요"),
    (3, 3, "HUM_MIXED", "건조와 과습이 반복됐어요",
     "건조 주의 3회, 과습 주의 3회 발생. 분무량과 환기 상태를 함께 점검해 주세요"),
    (1, 1, None, None, None),
])
def test_hum_case_boundaries(dry, over, code, title, line):
    r = run(humid("d", "env_humid_low", dry) + humid("o", "env_humid_high", over))
    if code is None:
        assert r.humid_card is None
        return
    c = r.humid_card
    assert (c.code, c.status, c.title, c.lines) == (code, "caution", title, [line])


def test_hum_cycles_none_excludes_delay_normal():
    delays = [ev(f"v{i}", "env_vent_delay", "caution", kst(3 + i, 15), kst(3 + i, 17),
                 cycle_started_at=iso(kst(3 + i, 9))) for i in range(2)]
    assert run(delays, cycles=None).humid_card is None
    assert run(delays, cycles=[cycle(kst(3, 9), 400)]).humid_card.code == "HUM_DELAY"


def test_hum_delay_and_dry_time():
    cycles = [cycle(kst(3, 9), 380), cycle(kst(4, 9), 360), cycle(kst(5, 9), None), cycle(kst(6, 9), None)]
    delays = [ev(f"v{i}", "env_vent_delay", "caution", kst(5 + i, 15), None,
                 cycle_started_at=iso(kst(5 + i, 9))) for i in range(2)]
    c = run(delays + humid("d", "env_humid_low", 3), cycles=cycles).humid_card
    assert c.code == "HUM_DELAY" and c.status == "danger"
    assert c.title == "사육장 환기가 지연되는 경향이 보여요"
    assert c.lines == ["총 2회 환기 지연이 발생. 건기 도달까지 평균 6시간 10분이 걸렸어요"]
    # 환기 지연 1회는 HUM_DELAY 아님 → 건조 3회가 대표
    assert run(delays[:1] + humid("d", "env_humid_low", 3), cycles=cycles).humid_card.code == "HUM_DRY"


def test_hum_normal_requires_cycles():
    assert run(cycles=[]).humid_card is None
    assert run(cycles=[cycle(kst(3, 9), None)]).humid_card is None               # 도달 0
    cycles = [cycle(kst(3, 9), 380), cycle(kst(4, 9), 360),
              cycle(kst(1, 9), 100)]                                            # 구간 전 시작은 제외
    c = run(cycles=cycles).humid_card
    assert c.code == "HUM_NORMAL" and c.status == "normal"
    assert c.title == "건습 사이클이 건강하게 유지되었어요"
    assert c.lines == ["건기 도달 평균 시간 6시간 10분으로 건습 사이클을 유지했어요"]


def test_dry_time_format():
    assert fmt_dry_time(380) == "6시간 20분"
    assert fmt_dry_time(360) == "6시간"
    assert fmt_dry_time(369.5) == "6시간 10분"      # half-up
    assert fmt_dry_time(45) == "45분"
