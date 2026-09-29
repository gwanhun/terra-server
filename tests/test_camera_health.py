"""camera_health — 카메라 진단값 이력(재시작 이벤트 + 10분 스냅샷) 테스트.

시간은 now(epoch) 인자로 주입해 결정론적으로 돌린다.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from backend import camera_health

CAM = "22222222-2222-2222-2222-bbbbbbbbbbbb"
T0 = 1_790_000_000.0

CLIPS = {
    "rec": 108, "skip": 26, "up_ok": 24, "up_fail": 84, "sd_backlog": 19,
    "last_rec_s": 3414, "last_err": {"err": 28674, "http": 0, "age_s": 3397, "stage": 2},
}


def _sys(uptime: int, reset: str = "PANIC") -> dict[str, Any]:
    return {"uptime_s": uptime, "reset": reset, "heap": 18788896, "rssi": -38}


def _sb(last_rows: list[dict] | None = None, insert_exc: Exception | None = None) -> MagicMock:
    """camera_health_events 조회(order→limit) 결과와 insert 동작을 지정한 mock."""
    sb = MagicMock()
    t = sb.table.return_value
    t.select.return_value.eq.return_value.order.return_value.limit.return_value \
        .execute.return_value.data = last_rows or []
    if insert_exc is not None:
        t.insert.return_value.execute.side_effect = insert_exc
    return sb


def _inserted(sb: MagicMock) -> list[dict]:
    return [c.args[0] for c in sb.table.return_value.insert.call_args_list]


@pytest.fixture(autouse=True)
def _clean() -> None:
    camera_health.reset()
    yield
    camera_health.reset()


def test_first_observation_without_history_writes_snapshot() -> None:
    sb = _sb()
    camera_health.record(sb, CAM, _sys(11738), CLIPS, "fb2-p4 0.2.0", now=T0)

    rows = _inserted(sb)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "snapshot"
    assert row["camera_id"] == CAM
    assert row["uptime_s"] == 11738
    assert row["reset"] == "PANIC"
    assert row["up_ok"] == 24 and row["up_fail"] == 84
    assert row["sd_backlog"] == 19 and row["last_rec_s"] == 3414
    assert row["last_err"] == CLIPS["last_err"]
    assert row["heap"] == 18788896 and row["rssi"] == -38
    assert row["fw"] == "fb2-p4 0.2.0"
    assert row["stats"] == CLIPS                      # skip/rec 등 원본도 보존
    sb.table.assert_any_call("camera_health_events")


def test_snapshot_every_10_minutes_within_same_boot() -> None:
    sb = _sb()
    camera_health.record(sb, CAM, _sys(1000), CLIPS, None, now=T0)
    camera_health.record(sb, CAM, _sys(1015), CLIPS, None, now=T0 + 15)
    camera_health.record(sb, CAM, _sys(1590), CLIPS, None, now=T0 + 590)
    assert len(_inserted(sb)) == 1                    # 15초 heartbeat 전부 저장 금지

    camera_health.record(sb, CAM, _sys(1600), CLIPS, None, now=T0 + 600)
    rows = _inserted(sb)
    assert len(rows) == 2 and rows[1]["kind"] == "snapshot"


def test_uptime_drop_writes_reset_immediately() -> None:
    sb = _sb()
    camera_health.record(sb, CAM, _sys(5000, "SW:rotate"), CLIPS, None, now=T0)
    camera_health.record(sb, CAM, _sys(20, "SW:rtc_loop_stall"), CLIPS, None, now=T0 + 60)

    rows = _inserted(sb)
    assert [r["kind"] for r in rows] == ["snapshot", "reset"]
    assert rows[1]["reset"] == "SW:rtc_loop_stall"
    assert rows[1]["uptime_s"] == 20


def test_reset_restarts_snapshot_timer_and_is_recorded_once() -> None:
    sb = _sb()
    camera_health.record(sb, CAM, _sys(5000), CLIPS, None, now=T0)
    camera_health.record(sb, CAM, _sys(20), CLIPS, None, now=T0 + 300)       # reset
    camera_health.record(sb, CAM, _sys(35), CLIPS, None, now=T0 + 315)      # 같은 부팅
    camera_health.record(sb, CAM, _sys(600), CLIPS, None, now=T0 + 880)     # reset 후 580s
    assert [r["kind"] for r in _inserted(sb)] == ["snapshot", "reset"]

    camera_health.record(sb, CAM, _sys(620), CLIPS, None, now=T0 + 900)     # reset 후 600s
    assert [r["kind"] for r in _inserted(sb)] == ["snapshot", "reset", "snapshot"]


def test_heartbeat_jitter_is_not_a_reset() -> None:
    """uptime 과 서버 시계가 몇 초 어긋나도(전송 지연) 새 부팅으로 보지 않는다."""
    sb = _sb()
    camera_health.record(sb, CAM, _sys(1000), CLIPS, None, now=T0)
    camera_health.record(sb, CAM, _sys(1010), CLIPS, None, now=T0 + 25)     # boot 15s 늦게 보임
    assert [r["kind"] for r in _inserted(sb)] == ["snapshot"]


def test_bridge_restart_detects_reboot_from_db_history() -> None:
    """브리지가 꺼진 동안 카메라가 재부팅했으면, 첫 관측에서 DB 마지막 행과 부팅 시각을 비교해 잡는다."""
    last_at = T0 - 3600                                    # 1시간 전 행: 그때 uptime 50000
    sb = _sb(last_rows=[{"at": _iso(last_at), "uptime_s": 50000}])
    camera_health.record(sb, CAM, _sys(900, "PANIC"), CLIPS, None, now=T0)

    rows = _inserted(sb)
    assert [r["kind"] for r in rows] == ["reset"]


def test_bridge_restart_same_boot_is_snapshot_not_reset() -> None:
    last_at = T0 - 3600
    sb = _sb(last_rows=[{"at": _iso(last_at), "uptime_s": 50000}])
    camera_health.record(sb, CAM, _sys(53600), CLIPS, None, now=T0)

    assert [r["kind"] for r in _inserted(sb)] == ["snapshot"]


def test_no_uptime_and_no_clips_writes_nothing() -> None:
    sb = _sb()
    camera_health.record(sb, CAM, None, None, None, now=T0)
    assert _inserted(sb) == []


def test_clips_without_sys_still_snapshots() -> None:
    """uptime 을 안 보내는 펌웨어도 업로드 추세 스냅샷은 남긴다(재시작 판정만 불가)."""
    sb = _sb()
    camera_health.record(sb, CAM, None, CLIPS, None, now=T0)
    rows = _inserted(sb)
    assert len(rows) == 1 and rows[0]["kind"] == "snapshot" and rows[0]["uptime_s"] is None


def test_insert_failure_is_swallowed() -> None:
    sb = _sb(insert_exc=RuntimeError("network down"))
    camera_health.record(sb, CAM, _sys(1000), CLIPS, None, now=T0)   # raise 하면 안 됨


def test_missing_table_pauses_writes() -> None:
    """migration 미적용(테이블 없음)이면 TTL 동안 INSERT 를 멈춰 카메라 수×주기 실패 스팸을 막는다."""
    missing = APIError({"message": "Could not find the table 'public.camera_health_events' "
                                   "in the schema cache", "code": "PGRST205",
                        "details": None, "hint": None})
    sb = _sb(insert_exc=missing)
    camera_health.record(sb, CAM, _sys(1000), CLIPS, None, now=T0)
    camera_health.record(sb, CAM, _sys(10), CLIPS, None, now=T0 + 60)       # reset 이어도 멈춤
    assert sb.table.return_value.insert.call_count == 1

    camera_health.record(sb, CAM, _sys(700), CLIPS, None,
                         now=T0 + 60 + camera_health.MISSING_TABLE_TTL_SEC + 1)
    assert sb.table.return_value.insert.call_count == 2


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def test_late_processed_heartbeat_is_not_a_reset() -> None:
    """리뷰 지적: 브리지가 DB 지연으로 heartbeat 를 늦게 처리해도(uptime 은 계속 증가) 재시작이 아니다."""
    sb = _sb()
    camera_health.record(sb, CAM, _sys(1000), CLIPS, None, now=T0)
    camera_health.record(sb, CAM, _sys(1015), CLIPS, None, now=T0 + 15)
    camera_health.record(sb, CAM, _sys(1030), CLIPS, None, now=T0 + 130)   # 100초 늦게 처리
    camera_health.record(sb, CAM, _sys(1135), CLIPS, None, now=T0 + 135)
    assert [r["kind"] for r in _inserted(sb)] == ["snapshot"]
