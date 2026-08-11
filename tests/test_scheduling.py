"""scheduling.compute_next_run 단위 테스트 (순수 함수, KST 환산 검증)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.scheduling import KST, compute_next_run, parse_time_of_day


def _utc(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ---------- parse_time_of_day ----------

def test_parse_hhmm() -> None:
    t = parse_time_of_day("08:30")
    assert (t.hour, t.minute, t.second) == (8, 30, 0)


def test_parse_hhmmss_from_db() -> None:
    t = parse_time_of_day("08:30:15")
    assert (t.hour, t.minute, t.second) == (8, 30, 15)


@pytest.mark.parametrize("bad", ["8", "25:00", "08:99", "abc"])
def test_parse_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_time_of_day(bad)


# ---------- daily ----------

def test_daily_later_today() -> None:
    # 지금 KST 07:00 (UTC 전날 22:00), 예약 08:00 → 오늘 KST 08:00 = UTC 23:00 전날
    now = _utc(2026, 8, 10, 22, 0)  # KST 8/11 07:00
    nxt = compute_next_run(now, "daily", parse_time_of_day("08:00"))
    # KST 8/11 08:00 == UTC 8/10 23:00
    assert nxt == _utc(2026, 8, 10, 23, 0)
    assert nxt.astimezone(KST).hour == 8


def test_daily_already_passed_today_goes_tomorrow() -> None:
    # KST 8/11 09:00 (UTC 8/11 00:00), 예약 08:00 이미 지남 → 내일 08:00
    now = _utc(2026, 8, 11, 0, 0)
    nxt = compute_next_run(now, "daily", parse_time_of_day("08:00"))
    # KST 8/12 08:00 == UTC 8/11 23:00
    assert nxt == _utc(2026, 8, 11, 23, 0)
    assert nxt.astimezone(KST).day == 12


def test_daily_always_future() -> None:
    now = _utc(2026, 8, 10, 12, 0)
    nxt = compute_next_run(now, "daily", parse_time_of_day("00:00"))
    assert nxt > now


# ---------- weekly ----------

def test_weekly_next_matching_day() -> None:
    # 2026-08-10 는 월요일. 예약: 수(3), KST 08:00.
    now = _utc(2026, 8, 10, 0, 0)  # KST 8/10 09:00 월
    nxt = compute_next_run(now, "weekly", parse_time_of_day("08:00"), [3])
    nxt_kst = nxt.astimezone(KST)
    assert nxt_kst.isoweekday() == 3  # 수요일
    assert nxt_kst.day == 12  # 8/12 수
    assert nxt_kst.hour == 8


def test_weekly_today_matches_but_time_passed_next_week() -> None:
    # 월요일(8/10) KST 09:00, 예약 월(1) 08:00 → 이미 지남 → 다음 주 월 8/17
    now = _utc(2026, 8, 10, 0, 0)  # KST 8/10 09:00 월
    nxt = compute_next_run(now, "weekly", parse_time_of_day("08:00"), [1])
    nxt_kst = nxt.astimezone(KST)
    assert nxt_kst.isoweekday() == 1
    assert nxt_kst.day == 17


def test_weekly_picks_earliest_of_multiple() -> None:
    # 월(8/10) KST 09:00, 예약 [월,수,금] 20:00 → 오늘 월 20:00 아직 안 지남
    now = _utc(2026, 8, 10, 0, 0)  # KST 8/10 09:00
    nxt = compute_next_run(now, "weekly", parse_time_of_day("20:00"), [1, 3, 5])
    nxt_kst = nxt.astimezone(KST)
    assert nxt_kst.day == 10 and nxt_kst.hour == 20


def test_weekly_requires_days() -> None:
    now = _utc(2026, 8, 10, 0, 0)
    with pytest.raises(ValueError):
        compute_next_run(now, "weekly", parse_time_of_day("08:00"), [])


def test_invalid_kind() -> None:
    now = _utc(2026, 8, 10, 0, 0)
    with pytest.raises(ValueError):
        compute_next_run(now, "monthly", parse_time_of_day("08:00"))
