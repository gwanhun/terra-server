"""schedule_runner.run_once 단위 테스트.

Supabase fluent chain mock. due 예약 → schedules UPDATE(next_run_at advance) +
commands INSERT 를 검증.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend import schedule_runner

DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"
OWNER_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _inject_sb(monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock):
    monkeypatch.setattr(schedule_runner, "get_supabase_client", lambda: fake_sb)
    yield


def _due_row(**over) -> dict:
    row = {
        "id": "sch-1",
        "device_id": DEVICE_UUID,
        "owner_id": OWNER_UUID,
        "action": "mist",
        "payload": {"duration_ms": 2000},
        "kind": "daily",
        "time_of_day": "08:00",
        "days_of_week": None,
    }
    row.update(over)
    return row


def test_no_due_schedules(fake_sb: MagicMock) -> None:
    (
        fake_sb.table.return_value.select.return_value
        .eq.return_value.lte.return_value.order.return_value
        .limit.return_value.execute.return_value.data
    ) = []
    assert schedule_runner.run_once() == 0


def test_due_schedule_advances_and_inserts_command(fake_sb: MagicMock) -> None:
    row = _due_row()
    updates: list[dict] = []
    inserts: list[dict] = []

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            # SELECT due
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [row]

            # UPDATE (advance) 캡처
            def _upd(payload: dict) -> MagicMock:
                updates.append(payload)
                chain = MagicMock()
                chain.eq.return_value.execute.return_value.data = [{"id": "sch-1"}]
                return chain
            t.update.side_effect = _upd
        elif name == "commands":
            def _ins(payload: dict) -> MagicMock:
                inserts.append(payload)
                chain = MagicMock()
                chain.execute.return_value.data = [{"id": "cmd-1"}]
                return chain
            t.insert.side_effect = _ins
        return t

    fake_sb.table.side_effect = _table

    fired = schedule_runner.run_once()
    assert fired == 1

    # 1) next_run_at advance + last_run_at 기록
    assert len(updates) == 1
    assert "next_run_at" in updates[0] and "last_run_at" in updates[0]

    # 2) commands INSERT — action/payload/issued_by 전달
    assert len(inserts) == 1
    cmd = inserts[0]
    assert cmd["device_id"] == DEVICE_UUID
    assert cmd["action"] == "mist"
    assert cmd["payload"] == {"duration_ms": 2000}
    assert cmd["issued_by"] == OWNER_UUID
    assert cmd["status"] == "pending"


def test_fire_advances_before_insert_failure_isolated(fake_sb: MagicMock) -> None:
    """한 예약 발화 실패해도 run_once 는 예외 전파 없이 카운트만 반영."""
    bad = _due_row(id="sch-bad", time_of_day="nope")  # parse 실패 유발

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "schedules":
            t.select.return_value.eq.return_value.lte.return_value.order.return_value.limit.return_value.execute.return_value.data = [bad]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"id": "sch-bad"}]
        return t

    fake_sb.table.side_effect = _table
    # 예외 없이 0 발화 (파싱 실패로 _fire_one 예외 → 잡힘)
    assert schedule_runner.run_once() == 0
