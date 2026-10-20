"""pending 24시간 정리 (Stage L Task 6)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError

from backend import recording_cleanup
from backend.recording_cleanup import PendingRecordingReaper, purge_one, purge_stale_pending
from tests.fake_sb import FakeSB

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
UID = "11111111-1111-1111-1111-111111111111"
CAM = "33333333-3333-3333-3333-333333333333"


def _row(rid: str, *, hours_ago: float, status: str = "pending", thumb: bool = True) -> dict[str, Any]:
    base = f"terra-clips/recordings/{UID}/{CAM}/{rid}"
    return {
        "id": rid, "user_id": UID, "camera_id": CAM, "client_id": f"c-{rid}", "status": status,
        "r2_key": f"{base}.mp4", "thumbnail_key": f"{base}.jpg" if thumb else None,
        "created_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def test_purges_pending_older_than_24h() -> None:
    sb = FakeSB(live_recordings=[
        _row("old-pending", hours_ago=25),
        _row("new-pending", hours_ago=23),
        _row("old-ready", hours_ago=25, status="ready"),
    ])
    deleted: list[str] = []
    n = purge_stale_pending(sb, NOW, deleted.append)
    assert n == 1
    assert sorted(r["id"] for r in sb.rows("live_recordings")) == ["new-pending", "old-ready"]
    base = f"terra-clips/recordings/{UID}/{CAM}/old-pending"
    assert deleted == [f"{base}.mp4", f"{base}.jpg"]


def test_purge_continues_on_delete_error(caplog: pytest.LogCaptureFixture) -> None:
    sb = FakeSB(live_recordings=[_row("a", hours_ago=30), _row("b", hours_ago=26, thumb=False)])
    calls: list[str] = []

    def _boom(key: str) -> None:
        calls.append(key)
        raise ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, "DeleteObject")

    with caplog.at_level("ERROR"):
        n = purge_stale_pending(sb, NOW, _boom)
    assert n == 2
    assert sb.rows("live_recordings") == []
    assert len(calls) == 3          # a.mp4, a.jpg, b.mp4 — 실패해도 다음으로
    assert "recording r2 delete failed key=" in caplog.text


class _FlipToReadyAfterSelect(FakeSB):
    """대상 조회(select) 직후 complete 가 끼어든 것처럼 행을 ready 로 바꾼다."""

    def table(self, name: str) -> Any:
        q = super().table(name)
        orig = q.execute
        store = self.store

        def _execute() -> Any:
            res = orig()
            if q._op is None:
                for r in store.get(name, []):
                    r["status"] = "ready"
            return res

        q.execute = _execute
        return q


def test_purge_skips_row_completed_meanwhile() -> None:
    sb = _FlipToReadyAfterSelect(live_recordings=[_row("raced", hours_ago=25)])
    deleted: list[str] = []
    n = purge_stale_pending(sb, NOW, deleted.append)
    assert n == 0
    assert [r["id"] for r in sb.rows("live_recordings")] == ["raced"]
    assert deleted == []


def test_purge_one_returns_false_when_row_not_pending() -> None:
    row = _row("r", hours_ago=25, status="ready")
    sb = FakeSB(live_recordings=[row])
    deleted: list[str] = []
    assert purge_one(sb, row, deleted.append) is False
    assert deleted == []


def test_reaper_runs_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = threading.Event()
    monkeypatch.setattr(recording_cleanup, "purge_stale_pending",
                        lambda sb, now, delete_fn: ran.set() or 0)
    reaper = PendingRecordingReaper(lambda: FakeSB(), lambda key: None, interval_sec=0.01)
    reaper.start()
    try:
        assert ran.wait(1.0)
        assert any(t.name == "recording-pending-reaper" for t in threading.enumerate())
    finally:
        reaper.stop()
