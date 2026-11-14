"""net_weak — 카메라 네트워크 신호 약함 안내 (Stage V).

왜 FakeSB: 안내 넣기·내리기는 app_notifications 행의 실제 상태(read_at, 행 수)로 확인해야 한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from backend import net_weak
from backend.net_weak import WeakTracker
from tests.fake_sb import FakeSB

CAM = "cam-1"
CAM_B = "cam-2"
OWNER = "aaaaaaaa-0000-0000-0000-000000000001"
TITLE = "네트워크 신호가 약하니 점검해 주세요"


def _feed(t, rssi, start, end, step=15):
    out = []
    for now in range(start, end + 1, step):
        out.append(t.observe(CAM, rssi, float(now)))
    return out


def test_tracker_sends_after_600s_of_weak():
    t = WeakTracker()
    assert set(_feed(t, -75, 0, 585)) == {None}
    assert t.observe(CAM, -80, 600.0) == "send"


def test_tracker_no_repeat_within_retry_window():
    t = WeakTracker()
    _feed(t, -80, 0, 600)
    assert set(_feed(t, -80, 615, 1185)) == {None}
    assert t.observe(CAM, -80, 1200.0) == "send"      # RETRY_SEC 뒤 재시도


def test_tracker_mark_sent_blocks_for_24h():
    t = WeakTracker()
    _feed(t, -80, 0, 600)
    t.mark_sent(CAM, 600.0)
    assert set(_feed(t, -80, 615, 86985)) == {None}    # 600 + 86400 = 87000 전까지
    assert t.observe(CAM, -80, 87000.0) == "send"


def test_tracker_minus_74_keeps_streak_and_does_not_clear():
    t = WeakTracker()
    _feed(t, -80, 0, 300)
    assert t.observe(CAM, -74, 315.0) is None          # 해제도 아니고(-70 미만) 지속도 안 끊는다
    assert set(_feed(t, -80, 330, 585)) == {None}
    assert t.observe(CAM, -80, 600.0) == "send"        # 0 부터 센 600초


def test_tracker_minus_70_resets_streak():
    t = WeakTracker()
    _feed(t, -80, 0, 300)
    assert t.observe(CAM, -70, 315.0) == "clear"       # 회복 샘플만 지속을 끊는다
    assert set(_feed(t, -80, 330, 915)) == {None}      # 330 부터 다시 600초
    assert t.observe(CAM, -80, 930.0) == "send"


def test_tracker_clears_at_minus_70_once():
    t = WeakTracker()
    t.mark_sent(CAM, 0.0)
    assert t.observe(CAM, -71, 15.0) is None
    assert t.observe(CAM, -70, 30.0) == "clear"
    assert t.observe(CAM, -60, 45.0) is None


def test_unknown_state_clears_once_on_good_sample():
    t = WeakTracker()                                  # 재시작 직후
    assert t.observe(CAM, -55, 0.0) == "clear"
    assert t.observe(CAM, -55, 15.0) is None


def test_clear_failed_retries_after_60s():
    t = WeakTracker()
    assert t.observe(CAM, -60, 0.0) == "clear"
    t.clear_failed(CAM, 0.0)                           # 해제 실패를 알림
    assert set(_feed(t, -60, 15, 45)) == {None}        # 60초 안에는 다시 시도 안 함
    assert t.observe(CAM, -60, 60.0) == "clear"


def test_gap_within_300s_keeps_streak():
    t = WeakTracker()
    t.observe(CAM, -80, 0.0)
    assert t.observe(CAM, -80, 300.0) is None
    assert t.observe(CAM, -80, 600.0) == "send"


def test_gap_over_300s_restarts_streak():
    t = WeakTracker()
    _feed(t, -80, 0, 585)
    assert t.observe(CAM, -80, 886.0) is None          # 585 → 886 = 301초 공백, 886 부터 다시
    assert set(_feed(t, -80, 901, 1471)) == {None}
    assert t.observe(CAM, -80, 1486.0) == "send"


# ---------- Task 2: 안내 넣기·내리기 (app_notifications) ----------


def _cam_row(cid: str = CAM, **extra) -> dict:
    return {"id": cid, "camera_id": f"p4cam-{cid}", "owner_id": OWNER,
            "superseded_at": None, "unlinked_at": None, **extra}


def _notice(cid: str = CAM, *, age: timedelta, read: bool, nid: str | None = None) -> dict:
    """이미 있는 안내 행. age = 생성 후 지난 시간, read=True 면 읽음 처리된 행."""
    created = datetime.now(timezone.utc) - age
    return {
        "id": nid or f"n-{cid}-{int(age.total_seconds())}", "user_id": OWNER,
        "kind": "camera.weak_signal", "category": "camera", "title": TITLE,
        "data": {"kind": "camera.weak_signal", "source": "external", "camera_id": cid, "rssi": -80},
        "created_at": created.isoformat(),
        "read_at": created.isoformat() if read else None,
    }


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> None:
    """스위치 켬 + 드라이런 끔(실제 INSERT/UPDATE)."""
    monkeypatch.setenv("NET_WEAK_NOTICE_ENABLED", "true")
    monkeypatch.setenv("NET_WEAK_NOTICE_DRY_RUN", "false")


def test_send_inserts_contract_row(live: None) -> None:
    sb = FakeSB(cameras=[_cam_row()])
    assert net_weak.send_notice(sb, CAM, -78) == "queued"
    rows = sb.rows("app_notifications")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "camera.weak_signal"
    assert row["category"] == "camera"
    assert row["title"] == "네트워크 신호가 약하니 점검해 주세요"
    assert row["route"] == "/crecam"
    assert row["source"] == "external"
    assert row["data"] == {"kind": "camera.weak_signal", "source": "external", "camera_id": CAM, "rssi": -78}
    assert row["dedupe_key"].startswith("net-weak:" + CAM + ":")
    assert row.get("read_at") is None
    assert row["user_id"] == OWNER
    # 계약 전문의 나머지: body 와 source_event_id(dedupe_key 끝과 같은 uuid)
    assert row["body"] == net_weak.BODY
    assert row["dedupe_key"] == "net-weak:" + CAM + ":" + row["source_event_id"]


def test_send_blocked_when_unread_exists(live: None) -> None:
    sb = FakeSB(cameras=[_cam_row()],
                app_notifications=[_notice(age=timedelta(hours=25), read=False)])
    assert net_weak.send_notice(sb, CAM, -78) == "unread"
    assert len(sb.rows("app_notifications")) == 1


def test_send_blocked_within_24h_of_read_notice(live: None) -> None:
    sb = FakeSB(cameras=[_cam_row()],
                app_notifications=[_notice(age=timedelta(hours=23, minutes=59), read=True)])
    assert net_weak.send_notice(sb, CAM, -78) == "recent"
    assert len(sb.rows("app_notifications")) == 1


def test_send_allowed_after_24h(live: None) -> None:
    sb = FakeSB(cameras=[_cam_row()],
                app_notifications=[_notice(age=timedelta(hours=24, minutes=1), read=True)])
    assert net_weak.send_notice(sb, CAM, -78) == "queued"
    assert len(sb.rows("app_notifications")) == 2


def test_many_other_camera_rows_do_not_hide_unread(live: None) -> None:
    others = [_notice(CAM_B, age=timedelta(minutes=i + 1), read=False, nid=f"b-{i}") for i in range(60)]
    sb = FakeSB(cameras=[_cam_row(), _cam_row(CAM_B)],
                app_notifications=[*others, _notice(age=timedelta(days=3), read=False)])
    assert net_weak.send_notice(sb, CAM, -78) == "unread"
    assert len(sb.rows("app_notifications")) == 61


def test_other_camera_not_blocked_nor_cleared(live: None) -> None:
    sb = FakeSB(cameras=[_cam_row(), _cam_row(CAM_B)],
                app_notifications=[_notice(CAM_B, age=timedelta(hours=1), read=False, nid="b-1")])
    assert net_weak.send_notice(sb, CAM, -78) == "queued"
    net_weak.clear_notices(sb, CAM)
    assert sb.one("app_notifications", id="b-1")["read_at"] is None


@pytest.mark.parametrize("col", ["unlinked_at", "superseded_at"])
def test_unlinked_or_superseded_camera_not_sent(live: None, col: str) -> None:
    sb = FakeSB(cameras=[_cam_row(**{col: "2026-11-10T00:00:00+00:00"})])
    assert net_weak.send_notice(sb, CAM, -78) == "no_owner"
    assert len(sb.rows("app_notifications")) == 0


def test_dry_run_logs_and_inserts_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("NET_WEAK_NOTICE_DRY_RUN", raising=False)
    sb = FakeSB(cameras=[_cam_row()])
    with caplog.at_level(logging.INFO, logger="backend.net_weak"):
        assert net_weak.send_notice(sb, CAM, -78) == "dry_run"
    assert len(sb.rows("app_notifications")) == 0
    assert any("net-weak intent" in r.getMessage() for r in caplog.records)


def test_clear_marks_only_unread_of_this_camera(live: None) -> None:
    old_read = _notice(age=timedelta(days=2), read=True, nid="read-1")
    sb = FakeSB(cameras=[_cam_row()],
                app_notifications=[old_read, _notice(age=timedelta(hours=1), read=False, nid="unread-1")])
    assert net_weak.clear_notices(sb, CAM) == 1
    assert sb.one("app_notifications", id="unread-1")["read_at"] is not None
    assert sb.one("app_notifications", id="read-1")["read_at"] == old_read["read_at"]


def test_flags_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NET_WEAK_NOTICE_ENABLED", raising=False)
    monkeypatch.delenv("NET_WEAK_NOTICE_DRY_RUN", raising=False)
    assert net_weak.enabled() is False
    assert net_weak.dry_run() is True
    monkeypatch.setenv("NET_WEAK_NOTICE_ENABLED", "on")
    assert net_weak.enabled() is True
    monkeypatch.setenv("NET_WEAK_NOTICE_DRY_RUN", "off")
    assert net_weak.dry_run() is False
    monkeypatch.setenv("NET_WEAK_NOTICE_DRY_RUN", "flase")   # 오타는 안전한 쪽(켜짐)
    assert net_weak.dry_run() is True


# ---------- Task 3: heartbeat 연결 (on_camera_rssi) ----------


class _Clock:
    """time.monotonic 대역 — 테스트가 시각을 직접 준다."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_tracker():
    net_weak.get_tracker().reset()
    yield
    net_weak.get_tracker().reset()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr("time.monotonic", c)
    return c


def _beat(sb, clock: _Clock, rssi: int, start: int, end: int, step: int = 15) -> None:
    """start~end 초를 15초 간격 heartbeat 로 흘린다."""
    for now in range(start, end + 1, step):
        clock.now = float(now)
        net_weak.on_camera_rssi(sb, CAM, "p4cam-" + CAM, rssi)


def test_on_camera_rssi_disabled_does_nothing(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.delenv("NET_WEAK_NOTICE_ENABLED", raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(net_weak, "send_notice", lambda *a: calls.append(a) or "queued")
    sb = FakeSB(cameras=[_cam_row()])
    _beat(sb, clock, -80, 0, 1200)
    assert calls == []
    assert net_weak.get_tracker()._states == {}
    assert len(sb.rows("app_notifications")) == 0


def test_on_camera_rssi_sends_once_then_waits_24h(live: None, clock: _Clock) -> None:
    sb = FakeSB(cameras=[_cam_row()])
    _beat(sb, clock, -80, 0, 600)
    assert len(sb.rows("app_notifications")) == 1
    _beat(sb, clock, -80, 615, 600 + 23 * 3600)
    assert len(sb.rows("app_notifications")) == 1


def test_on_camera_rssi_clears_on_recovery(live: None, clock: _Clock) -> None:
    sb = FakeSB(cameras=[_cam_row()])
    _beat(sb, clock, -80, 0, 600)
    row = sb.rows("app_notifications")[0]
    assert row.get("read_at") is None
    _beat(sb, clock, -70, 615, 615)
    assert row["read_at"] is not None


def test_clear_failure_retries_on_next_good_sample(
    live: None, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [None, 1]
    calls: list[str] = []

    def _clear(_sb, camera_uuid: str):
        calls.append(camera_uuid)
        return results[len(calls) - 1]

    monkeypatch.setattr(net_weak, "clear_notices", _clear)
    sb = FakeSB(cameras=[_cam_row()])
    _beat(sb, clock, -60, 0, 45)
    assert calls == [CAM]                 # 첫 해제 실패 → 60초 안에는 다시 안 함
    _beat(sb, clock, -60, 60, 60)
    assert calls == [CAM, CAM]            # 60초 뒤 회복 샘플에서 다시
    _beat(sb, clock, -60, 75, 75)
    assert calls == [CAM, CAM]            # 성공 뒤에는 호출 없음


def test_clear_unexpected_error_still_retries(
    live: None, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB 오류가 아닌 예외로 해제가 죽어도(handlers 가 삼킴) 다음에 다시 시도한다."""
    calls: list[str] = []

    def _clear(_sb, camera_uuid: str):
        calls.append(camera_uuid)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 1

    monkeypatch.setattr(net_weak, "clear_notices", _clear)
    sb = FakeSB(cameras=[_cam_row()])
    with pytest.raises(RuntimeError):
        _beat(sb, clock, -60, 0, 0)
    _beat(sb, clock, -60, 60, 60)
    assert calls == [CAM, CAM]


@pytest.mark.parametrize("bad", [0, 5, -121])
def test_out_of_range_rssi_is_ignored(live: None, clock: _Clock, bad: int) -> None:
    """0·양수·말이 안 되게 낮은 값은 회복으로도 약함으로도 보지 않는다."""
    sb = FakeSB(cameras=[_cam_row()])
    _beat(sb, clock, -80, 0, 585)
    clock.now = 590.0
    net_weak.on_camera_rssi(sb, CAM, "p4cam-" + CAM, bad)
    _beat(sb, clock, -80, 600, 600)                       # 지속이 안 끊겼으니 600초째 발송
    rows = sb.rows("app_notifications")
    assert len(rows) == 1
    clock.now = 605.0
    net_weak.on_camera_rssi(sb, CAM, "p4cam-" + CAM, bad)
    assert rows[0].get("read_at") is None                 # 내려가지도 않는다


def test_unread_result_arms_clear(live: None, clock: _Clock) -> None:
    """재시작 상황: 안 읽은 행이 DB 에 있는데 트래커는 '안내 없음'으로 알고 있다."""
    sb = FakeSB(cameras=[_cam_row()],
                app_notifications=[_notice(age=timedelta(hours=2), read=False, nid="left-1")])
    net_weak.get_tracker().set_active(CAM, False)
    _beat(sb, clock, -80, 0, 600)                         # 600초째 send → "unread"
    assert len(sb.rows("app_notifications")) == 1
    assert sb.one("app_notifications", id="left-1")["read_at"] is None
    _beat(sb, clock, -60, 615, 615)
    assert sb.one("app_notifications", id="left-1")["read_at"] is not None
