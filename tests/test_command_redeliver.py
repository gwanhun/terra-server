"""command_redeliver — 예약 on/off 명령 재전달 (설계 P2-(a)).

시간은 now(epoch) 인자로 주입. INSERT 는 insert_pending_command 를 가로채 캡처한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend import command_redeliver

DEV = "11111111-1111-1111-1111-aaaaaaaaaaaa"
T0 = 1_790_000_000.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _row(action: str = "fan2_off", *, source: str = "schedule", issued: float = T0,
         payload: dict | None = None, cmd_id: str = "cmd-1") -> dict[str, Any]:
    return {
        "id": cmd_id, "device_id": DEV, "action": action, "payload": payload,
        "issued_at": _iso(issued), "ttl_sec": 10, "issued_by": "owner-1",
        "source": source, "source_id": "sch-1",
    }


@pytest.fixture(autouse=True)
def _clean() -> None:
    command_redeliver.reset()
    yield
    command_redeliver.reset()


@pytest.fixture
def inserted(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def _fake_insert(sb: Any, **kw: Any) -> dict:
        calls.append(kw)
        return {"id": f"new-{len(calls)}", **kw}

    monkeypatch.setattr(command_redeliver, "insert_pending_command", _fake_insert)
    return calls


def test_failed_schedule_off_is_redelivered_once_when_device_reports(inserted: list) -> None:
    """오프라인 중 fan2_off 가 expired → 기기 telemetry 복귀 시 1회 재발행, 그 뒤엔 없음."""
    command_redeliver.note_failure(_row(), "expired", now=T0 + 15)

    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 120)
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 123)

    assert len(inserted) == 1
    kw = inserted[0]
    assert kw["device_uuid"] == DEV
    assert kw["action"] == "fan2_off"
    assert kw["source"] == "restore"                 # 예약 푸시(source='schedule')가 또 나가지 않게
    assert kw["source_id"] == "sch-1"
    assert kw["issued_by"] == "owner-1"
    assert "cmd-1" in kw["reason"] and "expired" in kw["reason"]


def test_grace_exceeded_gives_up(inserted: list) -> None:
    command_redeliver.note_failure(_row(), "no_ack", now=T0 + 40)
    command_redeliver.on_telemetry(
        MagicMock(), DEV, {"fan2": "ON"}, now=T0 + command_redeliver.REDELIVER_GRACE_SEC + 1)
    assert inserted == []
    assert command_redeliver.pending_for(DEV) == {}    # 포기한 건 정리


def test_failure_noted_after_grace_is_ignored(inserted: list) -> None:
    command_redeliver.note_failure(
        _row(), "no_ack", now=T0 + command_redeliver.REDELIVER_GRACE_SEC + 5)
    assert command_redeliver.pending_for(DEV) == {}


def test_already_in_desired_state_is_dropped(inserted: list) -> None:
    """telemetry 가 이미 목표 상태(fan2=OFF)면 재발행하지 않는다 — ACK 만 유실된 경우."""
    command_redeliver.note_failure(_row(), "no_ack", now=T0 + 40)
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "OFF"}, now=T0 + 60)
    assert inserted == []
    assert command_redeliver.pending_for(DEV) == {}


def test_unknown_state_still_redelivers(inserted: list) -> None:
    """구 펌웨어처럼 상태 필드가 없으면(None) 확인 불가 → 재발행(절대 상태라 중복 무해)."""
    command_redeliver.note_failure(_row("led_on"), "unknown_device", now=T0 + 1)
    command_redeliver.on_telemetry(MagicMock(), DEV, {"led": None}, now=T0 + 30)
    assert [kw["action"] for kw in inserted] == ["led_on"]


@pytest.mark.parametrize("action", ["mist", "relay_on", "relay_off", "fan_toggle", "heater_on"])
def test_non_target_actions_are_not_redelivered(inserted: list, action: str) -> None:
    """mist(이중 분무 > 누락)·relay(펌프, owner 결정 09-29)·toggle(절대 상태 아님)은 제외."""
    command_redeliver.note_failure(_row(action), "expired", now=T0 + 15)
    command_redeliver.on_telemetry(MagicMock(), DEV, {}, now=T0 + 60)
    assert inserted == []


@pytest.mark.parametrize("source", ["manual", "timer", "guard", "restore"])
def test_only_schedule_source_is_redelivered(inserted: list, source: str) -> None:
    """수동 조작은 사용자가 실패를 이미 봤다 — 나중에 몰래 실행하지 않는다. restore 는 재전달의 재전달 방지."""
    command_redeliver.note_failure(_row(source=source), "expired", now=T0 + 15)
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 60)
    assert inserted == []


def test_timed_one_shot_is_not_redelivered(inserted: list) -> None:
    """duration_ms 가 있는 one-shot(팬 타이머)은 늦게 켜면 끝 시각이 밀린다 — schedule_restore 와 같은 제외."""
    command_redeliver.note_failure(_row("fan_on", payload={"duration_ms": 60000}), "expired",
                                   now=T0 + 15)
    assert command_redeliver.pending_for(DEV) == {}


def test_newer_command_for_same_actuator_cancels(inserted: list) -> None:
    """실패 뒤 같은 액추에이터에 다른 명령(수동 포함)이 나가면 그게 최신 의도 — 재전달 취소."""
    command_redeliver.note_failure(_row("fan2_off"), "expired", now=T0 + 15)
    command_redeliver.note_dispatch(DEV, "fan2_on")
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 60)
    assert inserted == []


def test_other_actuator_command_does_not_cancel(inserted: list) -> None:
    command_redeliver.note_failure(_row("fan2_off"), "expired", now=T0 + 15)
    command_redeliver.note_dispatch(DEV, "led_on")
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 60)
    assert [kw["action"] for kw in inserted] == ["fan2_off"]


def test_latest_failure_per_actuator_wins(inserted: list) -> None:
    command_redeliver.note_failure(_row("fan2_on", cmd_id="a", issued=T0), "expired", now=T0 + 15)
    command_redeliver.note_failure(_row("fan2_off", cmd_id="b", issued=T0 + 60), "expired",
                                   now=T0 + 75)
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 90)
    assert [kw["action"] for kw in inserted] == ["fan2_off"]


def test_insert_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(sb: Any, **kw: Any) -> dict:
        raise RuntimeError("db down")
    monkeypatch.setattr(command_redeliver, "insert_pending_command", _boom)
    command_redeliver.note_failure(_row(), "expired", now=T0 + 15)
    command_redeliver.on_telemetry(MagicMock(), DEV, {"fan2": "ON"}, now=T0 + 60)   # raise 금지


def test_bad_issued_at_is_ignored(inserted: list) -> None:
    row = _row()
    row["issued_at"] = "not-a-date"
    command_redeliver.note_failure(row, "expired", now=T0 + 15)
    assert command_redeliver.pending_for(DEV) == {}
