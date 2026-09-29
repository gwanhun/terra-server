"""camera_alerts — 카메라 부분 멈춤 감지 (설계 P3, owner 결정 09-29: alert 만, 푸시·자동 재부팅 없음).

heartbeat 값만으로 판정한다. 시간은 now(epoch) 인자로 주입.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from backend import camera_alerts

CAM = "22222222-2222-2222-2222-bbbbbbbbbbbb"
T0 = 1_790_000_000.0


@pytest.fixture(autouse=True)
def _clean() -> None:
    camera_alerts.reset()
    yield
    camera_alerts.reset()


class _Sb:
    """camera_alerts 테이블 mock — 활성 알림 조회·INSERT·resolve UPDATE 를 기록."""

    def __init__(self, active: list[dict] | None = None) -> None:
        self.active = list(active or [])
        self.inserts: list[dict] = []
        self.resolves: list[tuple[dict, str]] = []
        self.sb = MagicMock()
        self.sb.table.side_effect = self._table

    def _table(self, name: str) -> MagicMock:
        assert name == "camera_alerts"
        t = MagicMock()

        def _select(_cols: str) -> MagicMock:
            q = MagicMock()
            def _eq_cam(_c: str, cam: str) -> MagicMock:
                q2 = MagicMock()
                def _eq_kind(_k: str, kind: str) -> MagicMock:
                    q3 = MagicMock()
                    q3.is_.return_value.limit.return_value.execute.return_value.data = [
                        a for a in self.active if a["kind"] == kind]
                    return q3
                q2.eq.side_effect = _eq_kind
                return q2
            q.eq.side_effect = _eq_cam
            return q
        t.select.side_effect = _select

        def _insert(row: dict) -> MagicMock:
            self.inserts.append(row)
            self.active.append({"id": f"a{len(self.inserts)}", "kind": row["kind"],
                                "triggered_at": row["triggered_at"]})
            return MagicMock()
        t.insert.side_effect = _insert

        def _update(patch: dict) -> MagicMock:
            u = MagicMock()
            def _eq_cam(_c: str, cam: str) -> MagicMock:
                u2 = MagicMock()
                def _eq_kind(_k: str, kind: str) -> MagicMock:
                    self.resolves.append((patch, kind))
                    self.active = [a for a in self.active if a["kind"] != kind]
                    return MagicMock()
                u2.eq.side_effect = _eq_kind
                return u2
            u.eq.side_effect = _eq_cam
            return u
        t.update.side_effect = _update
        return t


def _hb(db: _Sb, now: float, *, uptime: int, reset: str = "POWERON",
        up_ok: int = 0, up_fail: int = 0, last_rec_s: int = 10) -> None:
    sys_state = {"uptime_s": uptime, "reset": reset}
    clips: dict[str, Any] = {"up_ok": up_ok, "up_fail": up_fail, "last_rec_s": last_rec_s}
    camera_alerts.evaluate(db.sb, CAM, "p4cam-x", sys_state, clips, now=now)


# ---------- 비정상 재시작 ----------


@pytest.mark.parametrize("reason", [
    "PANIC", "INT_WDT", "TASK_WDT", "WDT",
    # 펌웨어 자체 워치독 (이관훈 회신 09-29 — 0.1.0 은 rtc/mqtt/cam, 0.2.0 은 net/boot_net/upload 추가)
    "SW:rtc_loop_stall", "SW:mqtt_stuck", "SW:net_wd", "SW:boot_net_wd", "SW:upload_stuck",
    "SW:cam_stall",
    "SW:rtc_send_stall",   # WebRTC 계열은 "rtc_loop_stall 등" — 송신/락 정지 사유 이름 미확정이라 prefix 로
])
def test_abnormal_reset_raises_alert(reason: str) -> None:
    db = _Sb()
    _hb(db, T0, uptime=5000)
    _hb(db, T0 + 60, uptime=20, reset=reason)

    assert [r["kind"] for r in db.inserts] == ["camera_abnormal_reset"]
    row = db.inserts[0]
    assert row["camera_id"] == CAM
    assert row["context"]["reset"] == reason
    assert row["severity"] == "warning"


@pytest.mark.parametrize("reason", ["POWERON", "SW:rotate", "SW:mqtt_reboot", "BROWNOUT", None])
def test_normal_reset_does_not_alert(reason: str | None) -> None:
    """회전 설정·원격 재부팅·전원 재투입은 부분 멈춤 신호가 아니다(현장 C 12:14 SW:rotate)."""
    db = _Sb()
    _hb(db, T0, uptime=5000)
    _hb(db, T0 + 60, uptime=20, reset=reason)  # type: ignore[arg-type]
    assert db.inserts == []


def test_same_boot_reports_alert_once() -> None:
    """부팅 후 매 heartbeat 가 같은 reset 사유를 싣고 온다 — 부팅당 1건."""
    db = _Sb()
    _hb(db, T0, uptime=5000)
    for i in range(5):
        _hb(db, T0 + 60 + 15 * i, uptime=20 + 15 * i, reset="PANIC")
    assert len(db.inserts) == 1


def test_bridge_restart_long_uptime_is_not_a_new_crash() -> None:
    """브리지 재시작 직후 첫 관측이 오래 켜져 있던 카메라면 과거 부팅 사유라 알림 안 함."""
    db = _Sb()
    _hb(db, T0, uptime=11738, reset="PANIC")
    assert db.inserts == []


def test_bridge_restart_fresh_boot_is_a_crash() -> None:
    db = _Sb()
    _hb(db, T0, uptime=30, reset="PANIC")
    assert [r["kind"] for r in db.inserts] == ["camera_abnormal_reset"]


# ---------- 업로드 정체 ----------


def test_upload_stall_raises_alert_after_window() -> None:
    """업로드 성공이 30분+ 멈춘 채 실패만 3회+ 늘면 → 정체 알림 (현장 A: up_ok 24 / up_fail 84 형)."""
    db = _Sb()
    _hb(db, T0, uptime=1000, up_ok=24, up_fail=10)
    _hb(db, T0 + 600, uptime=1600, up_ok=24, up_fail=20)
    assert db.inserts == []                                  # 아직 창 미달
    _hb(db, T0 + camera_alerts.STALL_SEC, uptime=1000 + int(camera_alerts.STALL_SEC),
        up_ok=24, up_fail=30)
    assert [r["kind"] for r in db.inserts] == ["camera_upload_stalled"]
    ctx = db.inserts[0]["context"]
    assert ctx["up_fail_delta"] == 20 and ctx["up_ok"] == 24


def test_no_uploads_at_all_is_not_stalled() -> None:
    """게코가 안 움직여 녹화가 없으면(up_ok·up_fail 둘 다 정체, last_rec_s 만 증가) 알림 안 함 — last_rec_s 단독 금지."""
    db = _Sb()
    _hb(db, T0, uptime=1000, up_ok=5, up_fail=1, last_rec_s=100)
    _hb(db, T0 + 7200, uptime=8200, up_ok=5, up_fail=1, last_rec_s=7300)
    assert db.inserts == []


def test_few_failures_are_not_stalled() -> None:
    db = _Sb()
    _hb(db, T0, uptime=1000, up_ok=5, up_fail=1)
    _hb(db, T0 + 3600, uptime=4600, up_ok=5, up_fail=3)
    assert db.inserts == []


def test_successful_upload_resets_window() -> None:
    db = _Sb()
    _hb(db, T0, uptime=1000, up_ok=5, up_fail=0)
    _hb(db, T0 + 1500, uptime=2500, up_ok=6, up_fail=10)     # 성공 1건 → 창 재시작
    _hb(db, T0 + 2000, uptime=3000, up_ok=6, up_fail=14)
    assert db.inserts == []


def test_stall_alert_is_not_duplicated() -> None:
    db = _Sb()
    _hb(db, T0, uptime=1000, up_ok=5, up_fail=0)
    _hb(db, T0 + 1800, uptime=2800, up_ok=5, up_fail=5)
    _hb(db, T0 + 1815, uptime=2815, up_ok=5, up_fail=6)
    _hb(db, T0 + 3600, uptime=4600, up_ok=5, up_fail=9)
    assert len(db.inserts) == 1


def test_existing_active_alert_in_db_is_not_duplicated() -> None:
    """브리지 재시작으로 메모리가 비어도 DB 에 활성 알림이 있으면 새로 만들지 않는다."""
    db = _Sb(active=[{"id": "old", "kind": "camera_upload_stalled"}])
    _hb(db, T0, uptime=1000, up_ok=5, up_fail=0)
    _hb(db, T0 + 1800, uptime=2800, up_ok=5, up_fail=5)
    assert db.inserts == []


def test_upload_success_resolves_active_alerts() -> None:
    """업로드가 다시 성공하면(up_ok 증가) 정체·재시작 알림 둘 다 회복 처리."""
    db = _Sb()
    _hb(db, T0, uptime=5000, up_ok=5, up_fail=0)
    _hb(db, T0 + 60, uptime=20, reset="PANIC", up_ok=0, up_fail=0)
    _hb(db, T0 + 60 + 1800, uptime=1820, reset="PANIC", up_ok=0, up_fail=4)
    assert {r["kind"] for r in db.inserts} == {"camera_abnormal_reset", "camera_upload_stalled"}

    _hb(db, T0 + 60 + 1815, uptime=1835, reset="PANIC", up_ok=1, up_fail=4)
    assert {k for _p, k in db.resolves} == {"camera_abnormal_reset", "camera_upload_stalled"}
    assert all("resolved_at" in p for p, _k in db.resolves)


def test_reboot_restarts_counters_without_false_stall() -> None:
    """재부팅으로 카운터가 0 으로 돌아가도 음수 델타로 오판하지 않는다."""
    db = _Sb()
    _hb(db, T0, uptime=5000, up_ok=24, up_fail=84)
    _hb(db, T0 + 60, uptime=20, reset="SW:rotate", up_ok=0, up_fail=0)
    _hb(db, T0 + 600, uptime=560, reset="SW:rotate", up_ok=0, up_fail=1)
    assert db.inserts == []


def test_missing_clips_is_ignored() -> None:
    db = _Sb()
    camera_alerts.evaluate(db.sb, CAM, "p4cam-x", {"uptime_s": 10, "reset": "PANIC"}, None, now=T0)
    # clips 없는 heartbeat 도 재시작 판정은 한다
    assert [r["kind"] for r in db.inserts] == ["camera_abnormal_reset"]


def test_db_failure_is_swallowed() -> None:
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("db down")
    camera_alerts.evaluate(sb, CAM, "p4cam-x", {"uptime_s": 10, "reset": "PANIC"},
                           {"up_ok": 0, "up_fail": 0}, now=T0)   # raise 금지


def test_sd_reupload_success_counts_as_upload() -> None:
    """구 펌웨어(현장 C: up_ok 0 / up_fail 4 / sd_ok 4)는 SD 재업로드로 올라간다 — 정체 아님."""
    db = _Sb()
    for i, (fail, sd_ok) in enumerate([(0, 0), (2, 1), (4, 2), (6, 3), (8, 4)]):
        camera_alerts.evaluate(db.sb, CAM, "p4cam-c", {"uptime_s": 1000 + 900 * i, "reset": "SW:rotate"},
                               {"up_ok": 0, "up_fail": fail, "sd_ok": sd_ok, "sd_fail": 0},
                               now=T0 + 900 * i)
    assert db.inserts == []


def test_sd_failures_count_toward_stall() -> None:
    db = _Sb()
    clips0 = {"up_ok": 3, "up_fail": 0, "sd_ok": 2, "sd_fail": 0}
    clips1 = {"up_ok": 3, "up_fail": 1, "sd_ok": 2, "sd_fail": 3}
    camera_alerts.evaluate(db.sb, CAM, "p4cam-x", {"uptime_s": 1000}, clips0, now=T0)
    camera_alerts.evaluate(db.sb, CAM, "p4cam-x", {"uptime_s": 2800}, clips1, now=T0 + 1800)
    assert [r["kind"] for r in db.inserts] == ["camera_upload_stalled"]


def test_insert_failure_retries_next_heartbeat() -> None:
    db = _Sb()
    real = db._table
    calls = {"n": 0}

    def _table(name: str) -> MagicMock:
        t = real(name)
        orig = t.insert.side_effect
        def _flaky(row: dict) -> MagicMock:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db down")
            return orig(row)
        t.insert.side_effect = _flaky
        return t

    db.sb.table.side_effect = _table
    _hb(db, T0, uptime=10, reset="PANIC")
    assert db.inserts == []
    _hb(db, T0 + 15, uptime=25, reset="PANIC")   # 부팅 첫 heartbeat 에서만 판정 → 유실되지 않게 재시도
    assert [r["kind"] for r in db.inserts] == ["camera_abnormal_reset"]
    _hb(db, T0 + 30, uptime=40, reset="PANIC")
    assert len(db.inserts) == 1


def test_second_crash_while_first_alert_active_is_recorded() -> None:
    """회복 전에 또 크래시하면 새 부팅의 크래시도 따로 남는다(이력용)."""
    db = _Sb()
    _hb(db, T0, uptime=10, reset="PANIC")
    _hb(db, T0 + 600, uptime=5, reset="TASK_WDT")
    assert [r["context"]["reset"] for r in db.inserts] == ["PANIC", "TASK_WDT"]


def test_bridge_restart_same_boot_reset_alert_not_duplicated() -> None:
    """브리지 재시작 직후(메모리 없음) 부팅 초반 heartbeat — 같은 부팅의 활성 알림이 DB 에 있으면 중복 안 만듦."""
    from datetime import datetime, timezone
    boot = T0 - 40
    db = _Sb(active=[{"id": "old", "kind": "camera_abnormal_reset",
                      "triggered_at": datetime.fromtimestamp(boot + 10, tz=timezone.utc).isoformat()}])
    _hb(db, T0, uptime=40, reset="PANIC")
    assert db.inserts == []
