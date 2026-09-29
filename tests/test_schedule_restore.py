"""schedule_restore — 재부팅 후 예약 상태 복원 (2026-09-28 앱 회신 §2).

Supabase fluent chain mock. schedules SELECT → 액추에이터별 마지막 이벤트 계산 → commands INSERT 검증.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from backend import schedule_restore
from backend.scheduling import KST

DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
OWNER_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _reset():
    schedule_restore.reset()
    yield
    schedule_restore.reset()


def _kst(h: int, mi: int = 0, *, y=2026, mo=9, d=28) -> datetime:
    """2026-09-28 은 월요일."""
    return datetime(y, mo, d, h, mi, tzinfo=KST).astimezone(timezone.utc)


def _sched(sid: str, action: str, tod: str, **over) -> dict:
    row = {"id": sid, "device_id": DEVICE_UUID, "owner_id": OWNER_UUID, "action": action,
           "payload": None, "kind": "daily", "time_of_day": tod, "days_of_week": None, "guard": None}
    row.update(over)
    return row


def _sb(rows: list[dict], inserts: list[dict], telemetry: dict | None = None) -> MagicMock:
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value.data = rows
        elif name == "commands":
            def _ins(p: dict) -> MagicMock:
                inserts.append(p)
                c = MagicMock()
                c.execute.return_value.data = [{"id": f"cmd-{len(inserts)}", **p}]
                return c
            t.insert.side_effect = _ins
        elif name == "telemetry":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = (
                [telemetry] if telemetry else [])
        return t
    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


# ---------- restore_after_reboot ----------

def test_led_on_restored_between_on_and_off() -> None:
    """조명 08:00 ON / 20:00 OFF, 14:00 재부팅 → led_on(밝기 포함) 1건, source=restore."""
    inserts: list[dict] = []
    sb = _sb([_sched("s-on", "led_on", "08:00", payload={"brightness": 70}),
              _sched("s-off", "led_off", "20:00")], inserts)

    queued = schedule_restore.restore_after_reboot(sb, DEVICE_UUID, "terra-x", now_utc=_kst(14))

    assert [q["action"] for q in queued] == ["led_on"]
    assert inserts == [{
        "device_id": DEVICE_UUID, "action": "led_on", "payload": {"brightness": 70},
        "issued_by": OWNER_UUID, "ttl_sec": 10, "status": "pending",
        "source": "restore", "source_id": "s-on", "reason": "reboot_restore",
    }]


def test_nothing_restored_after_off_event() -> None:
    """22:00 재부팅 → 마지막 이벤트가 OFF 라 큐잉 없음."""
    inserts: list[dict] = []
    sb = _sb([_sched("s-on", "led_on", "08:00"), _sched("s-off", "led_off", "20:00")], inserts)
    assert schedule_restore.restore_after_reboot(sb, DEVICE_UUID, "x", now_utc=_kst(22)) == []
    assert inserts == []


def test_on_at_exact_time_counts_as_past() -> None:
    inserts: list[dict] = []
    sb = _sb([_sched("s-on", "fan_on", "08:00"), _sched("s-off", "fan_off", "20:00")], inserts)
    schedule_restore.restore_after_reboot(sb, DEVICE_UUID, "x", now_utc=_kst(8, 0))
    assert [i["action"] for i in inserts] == ["fan_on"]


def test_each_actuator_independent() -> None:
    """조명은 켜져야 하고 냉각팬은 꺼져야 하는 시각 → led_on 만."""
    inserts: list[dict] = []
    sb = _sb([_sched("l-on", "led_on", "08:00"), _sched("l-off", "led_off", "20:00"),
              _sched("f-on", "fan2_on", "22:00"), _sched("f-off", "fan2_off", "06:00")], inserts)
    schedule_restore.restore_after_reboot(sb, DEVICE_UUID, "x", now_utc=_kst(14))
    assert sorted(i["action"] for i in inserts) == ["led_on"]


def test_weekly_uses_most_recent_matching_day() -> None:
    """월 08:00 ON(weekly) / 매일 20:00 OFF. 화 14:00 → 마지막은 월 20:00 OFF → 없음.
    월 14:00 → 월 08:00 ON 이 마지막 → 복원."""
    inserts: list[dict] = []
    rows = [_sched("w-on", "led_on", "08:00", kind="weekly", days_of_week=[1]),
            _sched("d-off", "led_off", "20:00")]
    schedule_restore.restore_after_reboot(_sb(rows, inserts), DEVICE_UUID, "x", now_utc=_kst(14, d=29))
    assert inserts == []
    schedule_restore.restore_after_reboot(_sb(rows, inserts), DEVICE_UUID, "x", now_utc=_kst(14, d=28))
    assert [i["action"] for i in inserts] == ["led_on"]


def test_one_shot_timer_schedule_excluded() -> None:
    """fan_on + duration_ms(팬 타이머)는 상태가 아니라 복원하지 않는다."""
    inserts: list[dict] = []
    sb = _sb([_sched("t", "fan_on", "08:00", payload={"duration_ms": 600000})], inserts)
    schedule_restore.restore_after_reboot(sb, DEVICE_UUID, "x", now_utc=_kst(14))
    assert inserts == []


def test_guard_skip_applies_to_restore() -> None:
    """skip_when_temp_above 가드가 걸린 조명 예약: 현재 온도가 높으면 복원도 스킵."""
    inserts: list[dict] = []
    guard = {"type": "skip_when_temp_above", "value": 30.0, "enabled": True}
    rows = [_sched("s-on", "led_on", "08:00", guard=guard), _sched("s-off", "led_off", "20:00")]
    schedule_restore.restore_after_reboot(
        _sb(rows, inserts, telemetry={"t_a": 32.0, "h_a": 50.0}), DEVICE_UUID, "x", now_utc=_kst(14))
    assert inserts == []
    schedule_restore.restore_after_reboot(
        _sb(rows, inserts, telemetry={"t_a": 25.0, "h_a": 50.0}), DEVICE_UUID, "x", now_utc=_kst(14))
    assert [i["action"] for i in inserts] == ["led_on"]


def test_restorable_actions_exclude_pump_mist_toggle() -> None:
    assert {"led_on", "led_off", "fan_on", "fan_off", "fan2_on", "fan2_off"} == set(schedule_restore.RESTORABLE_ACTIONS)
    for bad in ("mist", "relay_on", "relay_off", "led_toggle", "fan_toggle", "heater_on"):
        assert bad not in schedule_restore.RESTORABLE_ACTIONS


# ---------- note_uptime / maybe_restore (부팅 1회당 1번) ----------

def test_note_uptime_detects_reboot_once_per_boot() -> None:
    t0 = 1_000_000.0
    assert schedule_restore.note_uptime(DEVICE_UUID, 3600, now=t0) is False          # 첫 관측, 오래 켜짐
    assert schedule_restore.note_uptime(DEVICE_UUID, 3603, now=t0 + 3) is False      # 같은 부팅
    assert schedule_restore.note_uptime(DEVICE_UUID, 5, now=t0 + 60) is True         # 재부팅
    assert schedule_restore.note_uptime(DEVICE_UUID, 8, now=t0 + 63) is False        # 같은 부팅의 후속
    assert schedule_restore.note_uptime(DEVICE_UUID, 11, now=t0 + 66) is False


def test_note_uptime_late_processed_telemetry_is_not_a_reboot() -> None:
    """브리지가 DB 지연으로 telemetry 를 늦게 처리해도(uptime 은 계속 증가) 재부팅이 아니다.
    예전엔 부팅 시각(now - uptime)이 30초 넘게 밀려 보여 복원 ON 이 나가, 예약 구간 중 사용자가
    수동으로 끈 조명·팬이 다시 켜질 수 있었다(2026-09-29 카메라 리뷰에서 같은 결함 발견)."""
    t0 = 1_000_000.0
    assert schedule_restore.note_uptime(DEVICE_UUID, 3600, now=t0) is False
    assert schedule_restore.note_uptime(DEVICE_UUID, 3603, now=t0 + 3) is False
    assert schedule_restore.note_uptime(DEVICE_UUID, 3606, now=t0 + 66) is False     # 60초 늦게 처리
    assert schedule_restore.note_uptime(DEVICE_UUID, 3669, now=t0 + 69) is False


def test_note_uptime_bridge_restart_fresh_boot_only() -> None:
    """브리지 재시작 직후(직전 값 없음): uptime 이 짧을 때만 재부팅으로 본다."""
    assert schedule_restore.note_uptime("dev-a", 20, now=100.0) is True
    assert schedule_restore.note_uptime("dev-b", 600, now=100.0) is False


def test_maybe_restore_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(schedule_restore, "restore_after_reboot",
                        lambda sb, uuid, label, now_utc=None: calls.append(uuid) or [])
    sb = MagicMock()
    schedule_restore.maybe_restore(sb, DEVICE_UUID, "x", 7200)
    schedule_restore.maybe_restore(sb, DEVICE_UUID, "x", 4)     # 재부팅
    schedule_restore.maybe_restore(sb, DEVICE_UUID, "x", 7)
    schedule_restore.maybe_restore(sb, DEVICE_UUID, "x", 10)
    assert calls == [DEVICE_UUID]
