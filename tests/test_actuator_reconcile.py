"""actuator_reconcile — telemetry 실제 상태 vs 예약상 있어야 할 상태 재조정 (설계 P2-(b)).

schedules 는 schedule_restore 테스트와 같은 mock 체인, commands 는 최근 명령 조회 + INSERT 캡처.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend import actuator_reconcile
from backend.scheduling import KST

DEV = "11111111-1111-1111-1111-aaaaaaaaaaaa"
OWNER = "22222222-2222-2222-2222-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _reset():
    actuator_reconcile.reset()
    yield
    actuator_reconcile.reset()


def _kst(h: int, mi: int = 0) -> datetime:
    """2026-09-28(월) KST → UTC."""
    return datetime(2026, 9, 28, h, mi, tzinfo=KST).astimezone(timezone.utc)


def _sched(sid: str, action: str, tod: str, **over) -> dict:
    row = {"id": sid, "device_id": DEV, "owner_id": OWNER, "action": action, "payload": None,
           "kind": "daily", "time_of_day": tod, "days_of_week": None, "guard": None}
    row.update(over)
    return row


FAN2_SPAN = [_sched("on", "fan2_on", "08:00"), _sched("off", "fan2_off", "09:00")]


def _sb(schedules: list[dict], inserts: list[dict], recent: list[dict] | None = None,
        telemetry: dict | None = None, recent_exc: Exception | None = None) -> MagicMock:
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.eq.return_value.in_.return_value \
                .execute.return_value.data = schedules
        elif name == "commands":
            chain = (t.select.return_value.eq.return_value.in_.return_value.gte.return_value
                     .order.return_value.limit.return_value.execute)
            if recent_exc is not None:
                chain.side_effect = recent_exc
            else:
                chain.return_value.data = recent or []
            def _ins(p: dict) -> MagicMock:
                inserts.append(p)
                c = MagicMock()
                c.execute.return_value.data = [{"id": f"cmd-{len(inserts)}", **p}]
                return c
            t.insert.side_effect = _ins
        elif name == "telemetry":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value \
                .execute.return_value.data = [telemetry] if telemetry else []
        return t
    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


def _run(sb: MagicMock, state: dict, now: datetime, mono: float = 0.0) -> list[dict]:
    return actuator_reconcile.maybe_reconcile(sb, DEV, "terra-x", state, now_utc=now, mono=mono)


def test_fan2_left_on_after_off_event_is_turned_off_once() -> None:
    """09-29 사고형: 예약 OFF 구간인데 telemetry fan2=ON → 교정 fan2_off 1회."""
    inserts: list[dict] = []
    sb = _sb(FAN2_SPAN, inserts)

    _run(sb, {"fan2": "ON"}, _kst(9, 30), mono=0)
    _run(sb, {"fan2": "ON"}, _kst(9, 40), mono=actuator_reconcile.CHECK_INTERVAL_SEC + 1)

    assert len(inserts) == 1
    cmd = inserts[0]
    assert cmd["action"] == "fan2_off"
    assert cmd["source"] == "reconcile"
    assert cmd["source_id"] == "off"
    assert cmd["issued_by"] == OWNER
    assert cmd["status"] == "pending"
    assert "fan2" in cmd["reason"]


def test_matching_state_does_nothing() -> None:
    inserts: list[dict] = []
    _run(_sb(FAN2_SPAN, inserts), {"fan2": "OFF"}, _kst(9, 30))
    assert inserts == []


def test_on_segment_turns_on_with_schedule_payload() -> None:
    """예약 ON 구간인데 꺼져 있으면 ON (예약 payload — 밝기 등 — 그대로)."""
    inserts: list[dict] = []
    rows = [_sched("on", "led_on", "08:00", payload={"brightness": 60}),
            _sched("off", "led_off", "20:00")]
    _run(_sb(rows, inserts), {"led": "OFF"}, _kst(12))
    assert [(c["action"], c["payload"]) for c in inserts] == [("led_on", {"brightness": 60})]


def test_waits_for_settle_after_event() -> None:
    """이벤트 직후엔 예약 명령·재전달이 아직 진행 중일 수 있다 — SETTLE 전엔 손대지 않는다."""
    inserts: list[dict] = []
    sb = _sb(FAN2_SPAN, inserts)
    _run(sb, {"fan2": "ON"}, _kst(9, 2), mono=0)
    assert inserts == []
    _run(sb, {"fan2": "ON"}, _kst(9, 7), mono=actuator_reconcile.CHECK_INTERVAL_SEC + 1)
    assert [c["action"] for c in inserts] == ["fan2_off"]


def test_check_is_rate_limited_per_device() -> None:
    """3초 telemetry 마다 schedules 를 조회하지 않는다."""
    inserts: list[dict] = []
    sb = _sb(FAN2_SPAN, inserts)
    _run(sb, {"fan2": "OFF"}, _kst(9, 30), mono=0)
    _run(sb, {"fan2": "OFF"}, _kst(9, 30), mono=3)
    schedule_queries = [c for c in sb.table.call_args_list if c.args[0] == "schedules"]
    assert len(schedule_queries) == 1


@pytest.mark.parametrize("source", ["manual", "timer", "guard"])
def test_user_or_guard_action_since_event_is_respected(source: str) -> None:
    """예약 구간 중 사용자가 직접 바꿨거나(수동·타이머) 가드가 스킵했으면 그 구간 끝까지 덮지 않는다."""
    inserts: list[dict] = []
    recent = [{"source": source, "issued_at": _kst(8, 30).isoformat(), "action": "fan2_on"}]
    sb = _sb(FAN2_SPAN, inserts, recent=recent)
    _run(sb, {"fan2": "ON"}, _kst(9, 30), mono=0)
    _run(sb, {"fan2": "ON"}, _kst(10), mono=actuator_reconcile.CHECK_INTERVAL_SEC + 1)
    assert inserts == []


def test_recent_inflight_command_defers() -> None:
    """방금 restore/재전달 등이 나갔으면(SETTLE 안) 이번엔 건너뛰고 다음 확인 때 다시 본다."""
    inserts: list[dict] = []
    now = _kst(12)
    recent = [{"source": "restore", "issued_at": (now - timedelta(seconds=20)).isoformat(),
               "action": "fan2_off"}]
    sb = _sb(FAN2_SPAN, inserts, recent=recent)
    _run(sb, {"fan2": "ON"}, now, mono=0)
    assert inserts == []

    sb2 = _sb(FAN2_SPAN, inserts, recent=[])
    _run(sb2, {"fan2": "ON"}, now + timedelta(minutes=6),
         mono=actuator_reconcile.CHECK_INTERVAL_SEC + 1)
    assert [c["action"] for c in inserts] == ["fan2_off"]


def test_recent_command_query_failure_does_not_correct() -> None:
    """최근 명령을 못 읽으면 사용자 조작 여부를 모른다 — 안전하게 교정 안 함."""
    inserts: list[dict] = []
    _run(_sb(FAN2_SPAN, inserts, recent_exc=RuntimeError("db down")), {"fan2": "ON"}, _kst(9, 30))
    assert inserts == []


def test_stop_when_guard_is_not_forced_on() -> None:
    """stop_when_* 는 펌웨어가 조건 도달 시 스스로 끈다 — 다시 켜면 펌웨어와 싸운다."""
    inserts: list[dict] = []
    rows = [_sched("on", "fan_on", "08:00",
                   guard={"type": "stop_when_temp_below", "value": 25, "enabled": True}),
            _sched("off", "fan_off", "20:00")]
    _run(_sb(rows, inserts), {"fan": "OFF"}, _kst(12))
    assert inserts == []


def test_skip_guard_applies_to_on_correction() -> None:
    inserts: list[dict] = []
    rows = [_sched("on", "fan_on", "08:00",
                   guard={"type": "skip_when_humidity_above", "value": 70, "enabled": True}),
            _sched("off", "fan_off", "20:00")]
    _run(_sb(rows, inserts, telemetry={"h_a": 85.0, "t_a": 26.0}), {"fan": "OFF"}, _kst(12))
    assert inserts == []


def test_unknown_state_is_skipped() -> None:
    """구 펌웨어(fan2 필드 없음)는 실제 상태를 모른다 — 교정 안 함."""
    inserts: list[dict] = []
    _run(_sb(FAN2_SPAN, inserts), {"fan2": None}, _kst(9, 30))
    assert inserts == []


def test_relay_and_mist_are_never_reconciled() -> None:
    inserts: list[dict] = []
    rows = [_sched("r", "relay_on", "08:00"), _sched("m", "mist", "08:00")]
    _run(_sb(rows, inserts), {"relay": "OFF"}, _kst(12))
    assert inserts == []


def test_new_schedule_event_allows_one_more_correction() -> None:
    """한 이벤트당 1회. 다음 예약 이벤트가 오면 다시 1회 가능."""
    inserts: list[dict] = []
    rows = [_sched("on", "fan2_on", "08:00"), _sched("off", "fan2_off", "09:00"),
            _sched("on2", "fan2_on", "10:00"), _sched("off2", "fan2_off", "11:00")]
    sb = _sb(rows, inserts)
    _run(sb, {"fan2": "ON"}, _kst(9, 30), mono=0)
    _run(sb, {"fan2": "ON"}, _kst(9, 50), mono=1000)
    _run(sb, {"fan2": "ON"}, _kst(11, 30), mono=2000)
    assert [c["source_id"] for c in inserts] == ["off", "off2"]


def test_insert_failure_retries_next_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """INSERT 가 실패하면 그 이벤트를 '처리됨'으로 굳히지 않고 다음 확인 때 다시 시도."""
    inserts: list[dict] = []
    sb = _sb(FAN2_SPAN, inserts)
    real = actuator_reconcile.insert_pending_command
    calls = {"n": 0}

    def _flaky(sb_: object, **kw: object) -> dict | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return real(sb_, **kw)

    monkeypatch.setattr(actuator_reconcile, "insert_pending_command", _flaky)
    _run(sb, {"fan2": "ON"}, _kst(9, 30), mono=0)
    _run(sb, {"fan2": "ON"}, _kst(9, 40), mono=actuator_reconcile.CHECK_INTERVAL_SEC + 1)
    assert [c["action"] for c in inserts] == ["fan2_off"]
