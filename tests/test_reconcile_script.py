"""scripts/reconcile_r2_orphans.py 의 순수 로직 — 날짜 범위 기본값, 등록 클립 시각 집합(KST)."""

from __future__ import annotations

from datetime import date

from scripts.reconcile_r2_orphans import default_range, registered_started_at_keys
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
