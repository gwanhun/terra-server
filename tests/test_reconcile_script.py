"""scripts/reconcile_r2_orphans.py 의 순수 로직 — 날짜 범위 기본값, 등록 클립 시각 집합(KST)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from scripts.reconcile_r2_orphans import (
    DB_PAGE,
    default_range,
    find_dangling,
    registered_started_at_keys,
    split_in_flight,
)
from tests.fake_sb import FakeSB


def test_default_range_includes_today() -> None:
    assert default_range(2, today=date(2026, 11, 4)) == (date(2026, 11, 3), date(2026, 11, 4))
    assert default_range(1, today=date(2026, 11, 4)) == (date(2026, 11, 4), date(2026, 11, 4))
    assert default_range(0, today=date(2026, 11, 4)) == (date(2026, 11, 4), date(2026, 11, 4))


def test_registered_started_at_keys_kst_and_range() -> None:
    """UTC started_at 을 R2 키 날짜·시각(KST) 로 바꾸고, 범위는 KST 자정 경계로 자른다."""
    sb = FakeSB(motion_clips=[
        {"camera_id": "cam-1", "started_at": "2026-10-21T16:42:47+00:00"},   # KST 10-22 01:42:47
        {"camera_id": "cam-1", "started_at": "2026-10-20T15:00:00+00:00"},   # KST 10-21 00:00:00 (경계 포함)
        {"camera_id": "cam-1", "started_at": "2026-10-20T14:59:59+00:00"},   # KST 10-20 → 범위 밖
        {"camera_id": "cam-2", "started_at": "2026-10-21T16:42:47+00:00"},   # 다른 카메라
    ])
    out = registered_started_at_keys(sb, "cam-1", date(2026, 10, 21), date(2026, 10, 22))
    assert out == {"2026-10-22_014247", "2026-10-21_000000"}


def test_registered_started_at_keys_pages_past_1000() -> None:
    """PostgREST 1000행 캡 — range 로 끝까지 읽어야 2b(중복 재업로드 제외) 필터가 안 샌다."""
    base = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)   # KST 10-21 00:00
    rows = [{"camera_id": "cam-1", "started_at": (base + timedelta(seconds=i)).isoformat()}
            for i in range(DB_PAGE + 5)]
    out = registered_started_at_keys(FakeSB(motion_clips=rows), "cam-1", date(2026, 10, 21), date(2026, 10, 21))
    assert len(out) == DB_PAGE + 5
    assert "2026-10-21_001644" in out   # 1004초 뒤


def test_split_in_flight_skips_young_objects() -> None:
    now = datetime(2026, 11, 5, 4, 10, tzinfo=timezone.utc)
    mp4s = {
        "old.mp4": (100, now - timedelta(minutes=11)),
        "young.mp4": (200, now - timedelta(minutes=2)),
        "edge.mp4": (300, now - timedelta(minutes=10)),   # 정확히 min_age → 오래된 쪽
    }
    old, young = split_in_flight(mp4s, now, timedelta(minutes=10))
    assert old == {"old.mp4": 100, "edge.mp4": 300}
    assert young == ["young.mp4"]


def test_find_dangling_skips_present_and_young_rows() -> None:
    """R2 에 없는 행만, 단 생성 10분 미만은 제외(방어적)."""
    now = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "a", "r2_key": "k/a.mp4", "created_at": "2026-10-01T00:00:00+00:00"},   # 없음 → 대상
        {"id": "b", "r2_key": "k/b.mp4", "created_at": "2026-10-01T00:00:00+00:00"},   # 있음
        {"id": "c", "r2_key": "k/c.mp4", "created_at": "2026-11-05T01:55:00Z"},        # 없음이지만 5분 전 → 제외
    ]
    out = find_dangling(rows, {"k/b.mp4"}, now, timedelta(minutes=10))
    assert [r["id"] for r in out] == ["a"]
