"""live_session — 라이브 시청 제한 (owner 결정 2026-09-29).

- 한 번에 15분(카메라 기준 연속 시청 시간 — 가져오기·60초 안 재연결은 시계 이어짐)
- 15분 끝나면 5분 쉬어야 다시 봄 (서버 강제)
- 한 번에 한 기기. 다른 기기면 409(누가 보는지), takeover=true 면 기존 세션 끊고 가져옴
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend import live_session
from backend.live_session import LiveBusy, LiveCooldown

CAM = "33333333-3333-3333-3333-333333333333"
OWNER = "11111111-1111-1111-1111-111111111111"
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _row(**over: Any) -> dict[str, Any]:
    row = {"id": CAM, "camera_id": "p4cam-x", "live_session_id": None, "live_viewer_id": None,
           "live_viewer": None, "live_started_at": None, "live_ended_at": None,
           "live_cooldown_until": None, "stream_until": None}
    row.update(over)
    return row


class _Db:
    """cameras 한 행을 흉내 — claim 의 재조회와 조건부 UPDATE 를 기록."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.updates: list[tuple[dict, dict]] = []   # (patch, eq 조건)
        self.sb = MagicMock()
        self.sb.table.side_effect = self._table

    def _table(self, name: str) -> MagicMock:
        assert name == "cameras"
        t = MagicMock()
        t.select.return_value.eq.return_value.eq.return_value.limit.return_value \
            .execute.return_value.data = [dict(self.row)]

        def _update(patch: dict) -> MagicMock:
            conds: dict[str, Any] = {}

            class _Q:
                def eq(q, col: str, val: Any) -> "_Q":
                    conds[col] = val
                    return q

                def execute(q) -> MagicMock:
                    ok = all(self.row.get(c) == v for c, v in conds.items() if c != "owner_id")
                    self.updates.append((patch, dict(conds)))
                    if ok:
                        self.row.update(patch)
                    res = MagicMock()
                    res.data = [dict(self.row)] if ok else []
                    return res
            return _Q()
        t.update.side_effect = _update
        return t


def _claim(db: _Db, sid: str, vid: str | None = "dev-a", label: str | None = "iPhone 15",
           takeover: bool = False, now: datetime = T0) -> live_session.Claim:
    return live_session.claim(db.sb, CAM, OWNER, session_id=sid, viewer_id=vid,
                              viewer_label=label, takeover=takeover, now=now)


# ---------- 시작 / 15분 ----------


def test_first_claim_starts_15_minute_window() -> None:
    db = _Db(_row())
    c = _claim(db, "s1")
    assert c.previous_session is None
    assert c.live_until == T0 + timedelta(minutes=15)
    assert db.row["live_session_id"] == "s1"
    assert db.row["live_viewer_id"] == "dev-a"
    assert db.row["live_viewer"] == "iPhone 15"
    assert db.row["stream_mode"] == "webrtc"
    assert db.row["stream_until"] == _iso(T0 + timedelta(minutes=15))


def test_same_viewer_reconnect_keeps_clock() -> None:
    """같은 기기가 새 session 으로 재연결(stalled 후) — 시계는 이어지고 옛 세션은 닫을 대상."""
    db = _Db(_row())
    _claim(db, "s1")
    c = _claim(db, "s2", now=T0 + timedelta(minutes=10))
    assert c.previous_session == "s1"
    assert c.live_until == T0 + timedelta(minutes=15)


def test_reopen_within_60s_after_close_keeps_clock() -> None:
    """14분 보고 닫았다 바로 다시 열어 15분을 새로 받는 우회 방지 — 60초 안 재시작은 시계 이어짐."""
    db = _Db(_row())
    _claim(db, "s1")
    live_session.release(db.sb, CAM, OWNER, "s1", "closed", now=T0 + timedelta(minutes=14))
    c = _claim(db, "s2", now=T0 + timedelta(minutes=14, seconds=30))
    assert c.live_until == T0 + timedelta(minutes=15)


def test_reopen_after_gap_starts_new_window() -> None:
    db = _Db(_row())
    _claim(db, "s1")
    live_session.release(db.sb, CAM, OWNER, "s1", "closed", now=T0 + timedelta(minutes=5))
    later = T0 + timedelta(minutes=7)
    c = _claim(db, "s2", now=later)
    assert c.live_until == later + timedelta(minutes=15)


def test_continuing_clock_already_over_is_cooldown() -> None:
    db = _Db(_row())
    _claim(db, "s1")
    live_session.release(db.sb, CAM, OWNER, "s1", "closed", now=T0 + timedelta(minutes=14, seconds=59))
    with pytest.raises(LiveCooldown) as e:
        _claim(db, "s2", now=T0 + timedelta(minutes=15, seconds=30))
    assert e.value.retry_after == 270        # 15분 시점 + 5분 쉼 까지


# ---------- 5분 쉼 ----------


def test_cooldown_blocks_until_it_ends() -> None:
    db = _Db(_row(live_cooldown_until=_iso(T0 + timedelta(minutes=5))))
    with pytest.raises(LiveCooldown) as e:
        _claim(db, "s1", now=T0 + timedelta(minutes=1))
    assert e.value.retry_after == 240
    assert db.updates == []
    c = _claim(db, "s1", now=T0 + timedelta(minutes=5, seconds=1))
    assert c.live_until == T0 + timedelta(minutes=20, seconds=1)


def test_expired_but_not_yet_reaped_session_is_cooldown() -> None:
    """만료 스윕(15초 주기) 전에 온 요청도 쉼 시간 적용."""
    db = _Db(_row(live_session_id="s1", live_viewer_id="dev-a",
                  live_started_at=_iso(T0), stream_until=_iso(T0 + timedelta(minutes=15))))
    with pytest.raises(LiveCooldown) as e:
        _claim(db, "s1", now=T0 + timedelta(minutes=15, seconds=10))
    assert e.value.retry_after == 290


# ---------- 한 기기 ----------


def test_other_viewer_gets_busy_with_label() -> None:
    db = _Db(_row())
    _claim(db, "s1", vid="dev-a", label="iPhone 15")
    with pytest.raises(LiveBusy) as e:
        _claim(db, "s2", vid="dev-b", label="Galaxy S24", now=T0 + timedelta(minutes=3))
    assert e.value.viewer == "iPhone 15"
    assert db.row["live_session_id"] == "s1"


def test_takeover_moves_session_and_keeps_clock() -> None:
    """가져오기: 기존 세션은 닫을 대상, 시계는 카메라 기준으로 이어짐(기기 번갈아 무한 시청 방지)."""
    db = _Db(_row())
    _claim(db, "s1", vid="dev-a", label="iPhone 15")
    c = _claim(db, "s2", vid="dev-b", label="Galaxy S24", takeover=True,
               now=T0 + timedelta(minutes=3))
    assert c.previous_session == "s1"
    assert c.live_until == T0 + timedelta(minutes=15)
    assert db.row["live_session_id"] == "s2"
    assert db.row["live_viewer"] == "Galaxy S24"
    assert db.row["live_end_reason"] == "taken_over"


def test_legacy_app_without_viewer_id_is_not_single_viewer_limited() -> None:
    """구버전 앱(viewer_id 없음)끼리는 같은 시청자로 본다 — 시간 제한만 적용."""
    db = _Db(_row())
    _claim(db, "s1", vid=None, label=None)
    c = _claim(db, "s2", vid=None, label=None, now=T0 + timedelta(minutes=1))
    assert c.previous_session == "s1"
    assert db.row["live_viewer"] == live_session.LEGACY_LABEL


def test_new_app_sees_legacy_viewer_as_busy() -> None:
    db = _Db(_row())
    _claim(db, "s1", vid=None, label=None)
    with pytest.raises(LiveBusy) as e:
        _claim(db, "s2", vid="dev-b", label="iPhone 15", now=T0 + timedelta(minutes=1))
    assert e.value.viewer == live_session.LEGACY_LABEL


def test_label_is_trimmed() -> None:
    db = _Db(_row())
    _claim(db, "s1", label="  " + "x" * 100 + "  ")
    assert len(db.row["live_viewer"]) == live_session.VIEWER_LABEL_MAX


# ---------- 종료 ----------


def test_release_by_kicked_session_does_not_clear_new_session() -> None:
    """가져오기 당한 기기가 늦게 /close 를 보내도 새 시청자 세션을 지우면 안 된다."""
    db = _Db(_row())
    _claim(db, "s1", vid="dev-a")
    _claim(db, "s2", vid="dev-b", takeover=True, now=T0 + timedelta(minutes=1))
    assert live_session.release(db.sb, CAM, OWNER, "s1", "closed",
                                now=T0 + timedelta(minutes=2)) is False
    assert db.row["live_session_id"] == "s2"


def test_release_active_clears_without_cooldown() -> None:
    db = _Db(_row())
    _claim(db, "s1")
    assert live_session.release(db.sb, CAM, OWNER, "s1", "closed",
                                now=T0 + timedelta(minutes=2)) is True
    assert db.row["live_session_id"] is None
    assert db.row["stream_mode"] is None
    assert db.row["live_end_reason"] == "closed"
    assert db.row.get("live_cooldown_until") is None


# ---------- 만료 스윕 ----------


def _scan_then_row(db: _Db, scan: MagicMock) -> None:
    """첫 table() 호출(만료 대상 조회)은 scan, 이후(조건부 UPDATE)는 행 mock."""
    real_table = db._table
    calls = {"n": 0}

    def _table(name: str) -> MagicMock:
        calls["n"] += 1
        return scan if calls["n"] == 1 else real_table(name)
    db.sb.table.side_effect = _table


def test_expire_due_closes_camera_session_and_sets_cooldown() -> None:
    row = _row(live_session_id="s1", live_viewer_id="dev-a", live_viewer="iPhone 15",
               live_started_at=_iso(T0), stream_until=_iso(T0 + timedelta(minutes=15)))
    db = _Db(row)
    q = MagicMock()
    q.select.return_value.not_.is_.return_value.lt.return_value.limit.return_value \
        .execute.return_value.data = [dict(row)]
    _scan_then_row(db, q)
    closed: list[tuple[str, str]] = []

    n = live_session.expire_due(db.sb, lambda cam, sid: closed.append((cam, sid)),
                                now=T0 + timedelta(minutes=15, seconds=5))

    assert n == 1
    assert closed == [("p4cam-x", "s1")]
    assert db.row["live_session_id"] is None
    assert db.row["live_end_reason"] == "time_limit"
    assert db.row["live_cooldown_until"] == _iso(T0 + timedelta(minutes=20, seconds=5))


def test_expire_due_close_failure_still_ends_session() -> None:
    row = _row(live_session_id="s1", live_viewer_id="dev-a", live_started_at=_iso(T0),
               stream_until=_iso(T0 + timedelta(minutes=15)))
    db = _Db(row)
    q = MagicMock()
    q.select.return_value.not_.is_.return_value.lt.return_value.limit.return_value \
        .execute.return_value.data = [dict(row)]
    _scan_then_row(db, q)

    def _boom(cam: str, sid: str) -> None:
        raise RuntimeError("mqtt down")

    assert live_session.expire_due(db.sb, _boom, now=T0 + timedelta(minutes=16)) == 1
    assert db.row["live_session_id"] is None


# ---------- 가드 ----------


def test_all_written_columns_are_in_migrations() -> None:
    """claim/release/expire 가 cameras 에 쓰는 컬럼이 전부 migration 에 있어야 한다(9/17·9/21 사고 재발 방지)."""
    from tests.test_migration_coverage import _migration_columns

    db = _Db(_row())
    _claim(db, "s1")
    _claim(db, "s2", vid="dev-b", takeover=True, now=T0 + timedelta(minutes=1))
    live_session.release(db.sb, CAM, OWNER, "s2", "closed", now=T0 + timedelta(minutes=2))
    written = {k for patch, _ in db.updates for k in patch}
    written |= {"live_cooldown_until"}     # expire_due 전용
    assert written - _migration_columns("cameras") == set()


def test_reaper_runs_expire_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    ran = threading.Event()
    monkeypatch.setattr(live_session, "expire_due", lambda sb, close_fn: ran.set() or 0)
    reaper = live_session.LiveSessionReaper(lambda: MagicMock(), lambda cam, sid: None, interval_sec=0.01)
    reaper.start()
    try:
        assert ran.wait(1.0)
    finally:
        reaper.stop()
