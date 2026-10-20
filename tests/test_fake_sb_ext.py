"""FakeSB 확장(delete / lt) 테스트 — live_recordings 테스트가 기대는 동작."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.fake_sb import FakeSB


def test_delete_removes_matching_rows() -> None:
    sb = FakeSB(t=[{"id": "a", "status": "pending"}, {"id": "b", "status": "ready"},
                   {"id": "c", "status": "pending"}])
    res = sb.table("t").delete().eq("id", "a").eq("status", "pending").execute()
    assert res.data == [{"id": "a", "status": "pending"}]
    assert [r["id"] for r in sb.rows("t")] == ["b", "c"]

    # 조건 불일치면 0행 삭제
    res = sb.table("t").delete().eq("id", "b").eq("status", "pending").execute()
    assert res.data == []
    assert [r["id"] for r in sb.rows("t")] == ["b", "c"]


def test_lt_filters_iso_strings() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=25)).isoformat()
    new = (now - timedelta(hours=23)).isoformat()
    sb = FakeSB(t=[{"id": "old", "created_at": old}, {"id": "new", "created_at": new}])
    cutoff = (now - timedelta(hours=24)).isoformat()
    res = sb.table("t").select("*").lt("created_at", cutoff).execute()
    assert [r["id"] for r in res.data] == ["old"]


def test_gte_filters_iso_strings() -> None:
    base = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    sb = FakeSB(t=[
        {"id": "10", "t": base.isoformat()},
        {"id": "11", "t": (base + timedelta(hours=1)).isoformat()},
        {"id": "12", "t": (base + timedelta(hours=2)).isoformat()},
        {"id": "none", "t": None},
    ])
    res = sb.table("t").select("*").gte("t", (base + timedelta(hours=1)).isoformat()).execute()
    assert sorted(r["id"] for r in res.data) == ["11", "12"]
