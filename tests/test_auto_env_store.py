"""auto_env_store — Stage R 자동 환기·분무 DB 글루 테스트.

계획: specs/stage-r-auto-env.md Task 3 (R8 예약 구간, R11 복원, R12 상태 쓰기, R13 INSERT 실패·섀도,
Review Focus 4·5). 판정 규칙은 tests/test_auto_env.py 가 보고, 여기서는 명령 적재·섀도 로그·복원·
예약 구간·ACK 처리·상태 쓰기만 본다.

`_Step` 대신 같은 속성(sb, device_uuid, meta, now_mono, now_wall)을 가진 가짜 컨텍스트를 쓴다.
시계는 mono = 1000 + t, wall = T0 + t (T0 = 2026-11-05 12:00 KST).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend import auto_env_store as store
from backend.auto_env import KST
from backend.env_rules import Thresholds
from tests.fake_sb import FakeSB

DEV = "11111111-1111-1111-1111-aaaaaaaaaaaa"
OTHER = "22222222-2222-2222-2222-bbbbbbbbbbbb"
OWNER = "99999999-9999-9999-9999-999999999999"
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=KST)
TH = Thresholds(18.0, 27.0, 40.0, 80.0)
_UTC = timezone.utc


def at(t: float, base: datetime = T0) -> str:
    return (base + timedelta(seconds=t)).astimezone(_UTC).isoformat()


class SB(FakeSB):
    """FakeSB + 호출 기록(`calls`: (table, op)) + 실패 주입(`fail[(table, op)] = 남은 횟수`)."""

    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        tables.setdefault("devices", [{"id": DEV, "owner_id": OWNER}])
        tables.setdefault("commands", [])
        tables.setdefault("schedules", [])
        super().__init__(**tables)
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], int] = {}

    def table(self, name: str):  # type: ignore[override]
        q = super().table(name)
        orig = q.execute
        sb = self

        def execute():
            key = (name, q._op or "select")
            sb.calls.append(key)
            if sb.fail.get(key, 0) > 0:
                sb.fail[key] -= 1
                raise RuntimeError(f"boom {key}")
            return orig()

        q.execute = execute  # type: ignore[method-assign]
        return q

    def count(self, table: str, op: str | None = None) -> int:
        return sum(1 for t, o in self.calls if t == table and (op is None or o == op))


@dataclass
class Ctx:
    sb: Any
    device_uuid: str
    meta: dict[str, Any] | None
    now_mono: float
    now_wall: datetime


class Rig:
    def __init__(self, sb: SB, *, toggles: tuple[bool, bool] = (True, False), t0: datetime = T0) -> None:
        self.sb = sb
        self.st = store.AutoDeviceState()
        self.toggles = toggles
        self.t0 = t0
        self.t = 0.0

    def ctx(self) -> Ctx:
        return Ctx(sb=self.sb, device_uuid=DEV, meta={"id": DEV, "owner_id": OWNER},
                   now_mono=1000.0 + self.t, now_wall=self.t0 + timedelta(seconds=self.t))

    def run(self, secs: float, step: float = 3.0, *, temp: float | None = 24.0,
            humid: float | None = 60.0, fan: str | None = "OFF", relay: str | None = "OFF",
            wet: float | None = None) -> None:
        """[t, t+secs) 를 step 간격 샘플로. 끝나면 t = t+secs."""
        end = self.t + secs
        while self.t < end:
            row = {"device_id": DEV, "t_a": temp, "h_a": humid, "a_ok": True, "fan": fan, "relay": relay}
            store.step(self.ctx(), self.st, row, TH, self.toggles, wet)
            self.t += step

    def ack(self, row: dict[str, Any]) -> None:
        store.handle_ack(self.ctx(), self.st, row)

    def state(self) -> dict[str, Any]:
        return self.st.tracker.state_json(self.t0 + timedelta(seconds=self.t))


def cmds(sb: SB, device: str = DEV) -> list[dict[str, Any]]:
    return [r for r in sb.rows("commands") if r["device_id"] == device and r.get("_seed") is None]


def acked(row: dict[str, Any], result: str = "ok") -> dict[str, Any]:
    return {**row, "status": "acked", "result": result}


def cmd_row(action: str, source: str, t: float, *, status: str = "acked", result: str | None = "ok",
            reason: str | None = None, device: str = DEV) -> dict[str, Any]:
    return {"id": f"seed-{action}-{source}-{t}", "device_id": device, "action": action,
            "payload": None, "source": source, "status": status, "result": result, "reason": reason,
            "issued_at": at(t), "acked_at": at(t + 1) if status == "acked" else None, "_seed": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTO_ENV_DRY_RUN", "false")
    monkeypatch.delenv("AUTO_ENV_ENABLED", raising=False)
    monkeypatch.delenv("AUTO_ENV_DEVICES", raising=False)


def _state_row(sb: SB) -> dict[str, Any] | None:
    return sb.one("devices", id=DEV).get("auto_env_state")  # type: ignore[union-attr]


# ---------- 스위치 ----------

def test_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert store.auto_env_enabled() is False
    monkeypatch.setenv("AUTO_ENV_ENABLED", "true")
    assert store.auto_env_enabled() is True

    monkeypatch.delenv("AUTO_ENV_DRY_RUN")
    assert store.dry_run() is True               # 기본 켜짐
    for v in ("1", "true", "", "garbage", "ON"):
        monkeypatch.setenv("AUTO_ENV_DRY_RUN", v)
        assert store.dry_run() is True
    for v in ("0", "false", "No", " off "):
        monkeypatch.setenv("AUTO_ENV_DRY_RUN", v)
        assert store.dry_run() is False

    assert store.device_allowed(DEV) is True     # 비면 전체
    monkeypatch.setenv("AUTO_ENV_DEVICES", f" {OTHER} , {DEV} ")
    assert store.device_allowed(DEV) is True
    monkeypatch.setenv("AUTO_ENV_DEVICES", OTHER)
    assert store.device_allowed(DEV) is False


# ---------- 비용 ----------

def test_off_toggles_cost_nothing() -> None:
    sb = SB()
    rig = Rig(sb, toggles=(False, False))
    rig.run(600, humid=85.0)
    rig.run(600, humid=30.0)
    assert sb.calls == []
    assert rig.st.restored is False


# ---------- 명령 적재 ----------

def test_insert_auto_command() -> None:
    sb = SB()
    rig = Rig(sb)
    rig.run(177, humid=85.0)
    assert cmds(sb) == []
    rig.run(6, humid=85.0)
    rows = cmds(sb)
    assert len(rows) == 1
    r = rows[0]
    assert (r["action"], r["source"], r["issued_by"], r["reason"], r["status"]) == (
        "fan_on", "auto", OWNER, "auto_vent:humid_high", "pending")
    assert r["payload"] == {"duration_ms": 3_600_000}
    assert _state_row(sb)["vent"]["active"] is False     # ACK 전

    rig.ack(acked(r))
    st = _state_row(sb)
    assert st["vent"]["active"] is True and st["vent"]["causes"] == ["humid_high"]


def test_dry_run_logs_only(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("AUTO_ENV_DRY_RUN")   # 기본 = 섀도
    caplog.set_level(logging.INFO, logger="backend.auto_env_store")
    sb = SB()
    rig = Rig(sb)
    rig.run(183, humid=85.0)                 # 실제 팬 텔레메트리는 계속 OFF

    def intents() -> list[str]:
        return [r.getMessage() for r in caplog.records if "auto-env intent" in r.getMessage()]

    assert sb.rows("commands") == []
    assert intents() == [f"auto-env intent device={DEV} action=fan_on reason=auto_vent:humid_high"]
    assert "auto_env_state" not in sb.one("devices", id=DEV)    # 앱에 가짜 상태를 보이지 않음
    assert rig.st.tracker.phase == "owned"                      # 즉시 ACK ok 로 봄

    # 모의 팬이 켜져 있는 동안엔 실제 텔레메트리 OFF 를 외부 끔으로 보지 않는다. 1시간 뒤 모의 타이머 만료 → 재발행
    rig.run(3600 - 6, humid=85.0)
    assert len(intents()) == 1
    rig.run(12, humid=85.0)
    assert len(intents()) == 2 and intents()[1].endswith("action=fan_on reason=auto_vent:humid_high")
    assert sb.rows("commands") == []
    assert "auto_env_state" not in sb.one("devices", id=DEV)


def test_insert_failure_retries() -> None:
    sb = SB()
    sb.fail[("commands", "insert")] = 1
    rig = Rig(sb)
    rig.run(183, humid=85.0)                 # t=180 INSERT 실패
    assert cmds(sb) == []
    rig.run(207 - 183, humid=85.0)           # 30초 안
    assert cmds(sb) == []
    rig.run(6, humid=85.0)                   # t=210 재시도
    assert [r["action"] for r in cmds(sb)] == ["fan_on"]


# ---------- 예약 구간(R8) ----------

def _sched(pair: str, action: str, hhmm: str, *, enabled: bool = True, device: str = DEV) -> dict[str, Any]:
    return {"id": f"s-{pair}-{action}", "device_id": device, "owner_id": OWNER, "action": action,
            "payload": None, "kind": "daily", "time_of_day": f"{hhmm}:00", "days_of_week": None,
            "enabled": enabled, "pair_id": pair}


def test_schedule_span_pairs_union() -> None:
    rows = [
        _sched("A", "fan_on", "10:00"), _sched("A", "fan_off", "14:00"),
        _sched("B", "fan_on", "12:00"), _sched("B", "fan_off", "13:00"),
        _sched("C", "fan_on", "09:00"), _sched("C", "fan_off", "16:00", enabled=False),  # 한쪽 꺼짐 → 무시
        {**_sched("D", "fan_on", "09:00"), "pair_id": None},                             # 단발 → 무시
        _sched("E", "fan_on", "09:00", device=OTHER), _sched("E", "fan_off", "20:00", device=OTHER),
    ]
    sb = SB(schedules=rows)
    t0 = datetime(2026, 11, 5, 13, 30, tzinfo=KST)
    rig = Rig(sb, t0=t0)
    rig.run(9)
    assert _state_row(sb)["vent"]["schedule_hold"] is True       # A 켜짐(B 는 13시에 꺼짐) — 합집합

    rig.t = 90 * 60                                               # 15:00 — A·B 꺼짐, C 는 무효 pair
    rig.run(9)
    assert _state_row(sb)["vent"]["schedule_hold"] is False

    # 순수 함수도 직접(조회가 device_id 로 거른 뒤의 행)
    mine = [r for r in rows if r["device_id"] == DEV]
    assert store.span_active(mine, datetime(2026, 11, 5, 13, 30, tzinfo=KST)) is True
    assert store.span_active(mine, datetime(2026, 11, 5, 15, 0, tzinfo=KST)) is False
    assert store.span_active(mine, datetime(2026, 11, 5, 9, 30, tzinfo=KST)) is False


def test_schedule_reread_before_insert() -> None:
    sb = SB()
    rig = Rig(sb)
    rig.run(30, humid=60.0)                  # t=0 에 예약 캐시(빈 목록) 로드
    rig.run(165, humid=85.0)                 # t=30 부터 과습. t=180 에 캐시 재로드(여전히 빔)
    sb.rows("schedules").extend([_sched("A", "fan_on", "11:00"), _sched("A", "fan_off", "13:00")])
    rig.run(18, humid=85.0)                  # t=210 시작 판정 → INSERT 직전 재조회에서 구간 켜짐
    assert cmds(sb) == []
    rig.run(9, humid=85.0)
    assert cmds(sb) == []
    assert _state_row(sb)["vent"]["schedule_hold"] is True


# ---------- 복원(R11) ----------

def test_restore_last_writer() -> None:
    # 40분 전 자동 fan_on ok + 20분 전 수동 fan_on ok → 마지막 작성자는 수동 → 소유 없음
    sb = SB(commands=[
        cmd_row("fan_on", "auto", -2400, reason="auto_vent:humid_high"),
        cmd_row("fan_on", "manual", -1200),
    ])
    rig = Rig(sb)
    rig.run(90, humid=60.0, fan="ON")
    assert rig.st.tracker.phase == "idle"
    assert cmds(sb) == []

    # 40분 전 자동 fan_on ok 단독 → 소유 복원 → 원인 해소 60초 뒤 fan_off
    sb = SB(commands=[cmd_row("fan_on", "auto", -2400, reason="auto_vent:humid_high")])
    rig = Rig(sb)
    rig.run(3, humid=60.0, fan="ON")
    assert rig.st.tracker.phase == "owned"
    assert rig.state()["vent"]["causes"] == ["humid_high"]
    rig.run(54, humid=60.0, fan="ON")
    assert cmds(sb) == []
    rig.run(9, humid=60.0, fan="ON")
    assert [(r["action"], r["reason"]) for r in cmds(sb)] == [("fan_off", "auto_vent_end")]


def test_restore_ignores_failed_and_old_auto() -> None:
    # 자동 fan_on 이 busy 였거나 1시간보다 오래면 소유 아님. 그 뒤 실패한 수동 명령은 작성자가 아님
    sb = SB(commands=[cmd_row("fan_on", "auto", -1800, result="busy", reason="auto_vent:temp_high")])
    rig = Rig(sb)
    rig.run(3)
    assert rig.st.tracker.phase == "idle"

    sb = SB(commands=[cmd_row("fan_on", "auto", -3700, reason="auto_vent:temp_high")])
    rig = Rig(sb)
    rig.run(3)
    assert rig.st.tracker.phase == "idle"

    sb = SB(commands=[
        cmd_row("fan_on", "auto", -1800, reason="auto_vent:temp_high"),
        cmd_row("fan_off", "manual", -600, status="expired", result="expired"),
    ])
    rig = Rig(sb)
    rig.run(3)
    assert rig.st.tracker.phase == "owned"
    assert rig.state()["vent"]["causes"] == ["temp_high"]


def test_restore_state_columns() -> None:
    state = {
        "vent": {"active": False, "causes": [], "since": None, "paused_until": at(600),
                 "schedule_hold": False, "last_end_at": at(-300), "runtime_today_sec": 7200},
        "mist": {"last_at": None, "today_count": 2},
        "day": "2026-11-05", "updated_at": at(-300),
    }
    sb = SB(devices=[{"id": DEV, "owner_id": OWNER, "auto_env_state": state}])
    rig = Rig(sb, toggles=(True, True))
    rig.run(3)
    s = rig.state()
    assert s["vent"]["paused_until"] == at(600)
    assert s["vent"]["last_end_at"] == at(-300)
    assert s["vent"]["runtime_today_sec"] == 7200
    assert s["mist"]["today_count"] == 2

    # 어제 상태면 하루 값은 0
    old = {**state, "day": "2026-11-04"}
    sb = SB(devices=[{"id": DEV, "owner_id": OWNER, "auto_env_state": old}])
    rig = Rig(sb, toggles=(True, True))
    rig.run(3)
    s = rig.state()
    assert s["vent"]["runtime_today_sec"] == 0 and s["mist"]["today_count"] == 0


def test_restore_mist_from_midnight() -> None:
    sb = SB(commands=[
        cmd_row("mist", "auto", -11 * 3600),                       # 01:00 KST
        cmd_row("mist", "auto", -7 * 3600, status="no_ack", result="no_ack"),   # 시도는 시도
        cmd_row("mist", "auto", -3 * 3600),                        # 09:00
        cmd_row("mist", "auto", -13 * 3600),                       # 어제 23:00 — 오늘 수 아님
        cmd_row("mist", "manual", -1800),                          # 30분 전 수동 ok
        cmd_row("mist", "auto", -60, device=OTHER),                # 다른 기기
    ])
    rig = Rig(sb, toggles=(False, True))
    rig.run(3, humid=35.0)
    s = rig.state()
    assert s["mist"]["today_count"] == 3
    assert s["mist"]["last_at"] == at(-1800 + 1)                   # 수동 분무 acked_at
    rig.run(600, humid=35.0)
    assert cmds(sb) == []                                          # 2시간 간격 가드

    rig.t = 2 * 3600 - 1800 + 60                                   # 수동 분무 2시간 뒤
    rig.run(200, humid=35.0)
    assert [(r["action"], r["reason"]) for r in cmds(sb)] == [("mist", "auto_mist")]
    assert rig.state()["mist"]["today_count"] == 4


def test_restore_failure_defers() -> None:
    sb = SB()
    sb.fail[("commands", "select")] = 1
    rig = Rig(sb)
    rig.run(3, humid=85.0)
    assert rig.st.restored is False
    assert sb.count("devices", "update") == 0 and sb.count("schedules") == 0
    rig.run(3, humid=85.0)
    assert rig.st.restored is True
    assert sb.count("devices", "update") == 1


# ---------- ACK(R7) ----------

def test_manual_ack_supersedes_pending_auto() -> None:
    sb = SB(commands=[
        {"id": "p-off", "device_id": DEV, "action": "fan_off", "source": "auto", "status": "pending",
         "result": None},
        {"id": "p-mist", "device_id": DEV, "action": "mist", "source": "auto", "status": "pending",
         "result": None},
        {"id": "p-sched", "device_id": DEV, "action": "fan_off", "source": "schedule", "status": "pending",
         "result": None},
        {"id": "p-other", "device_id": OTHER, "action": "fan_off", "source": "auto", "status": "pending",
         "result": None},
        {"id": "s-off", "device_id": DEV, "action": "fan_off", "source": "auto", "status": "sent",
         "result": None},
    ])
    rig = Rig(sb)
    rig.run(3)
    rig.ack({"id": "m-1", "device_id": DEV, "action": "fan_on", "source": "manual",
             "status": "acked", "result": "ok"})
    by_id = {r["id"]: r for r in sb.rows("commands")}
    assert (by_id["p-off"]["status"], by_id["p-off"]["result"]) == ("expired", "superseded")
    for rid in ("p-mist", "p-sched", "p-other"):
        assert by_id[rid]["status"] == "pending", rid
    assert by_id["s-off"]["status"] == "sent"


def test_manual_off_ack_pauses_and_failed_manual_ignored() -> None:
    sb = SB()
    rig = Rig(sb)
    rig.run(183, humid=85.0)
    rig.ack(acked(cmds(sb)[0]))
    assert rig.st.tracker.phase == "owned"
    rig.ack({"id": "m-1", "device_id": DEV, "action": "fan_off", "source": "manual",
             "status": "acked", "result": "busy"})
    assert rig.st.tracker.phase == "owned"                     # 실패한 수동 명령은 무시
    rig.ack({"id": "m-2", "device_id": DEV, "action": "fan_off", "source": "manual",
             "status": "acked", "result": "ok"})
    assert rig.st.tracker.phase == "idle"
    assert _state_row(sb)["vent"]["paused_until"] is not None


def test_own_ack_failure_no_ownership() -> None:
    sb = SB()
    rig = Rig(sb)
    rig.run(183, humid=85.0)
    rig.ack(acked(cmds(sb)[0], result="busy"))
    assert rig.st.tracker.phase == "idle"


def test_mist_ack_any_source_feeds_guard() -> None:
    sb = SB()
    rig = Rig(sb, toggles=(False, True))
    rig.run(3, humid=50.0)
    rig.ack({"id": "m-1", "device_id": DEV, "action": "mist", "source": "manual",
             "status": "acked", "result": "ok"})
    assert rig.state()["mist"]["last_at"] is not None
    rig.run(600, humid=35.0)
    assert cmds(sb) == []                                      # 2시간 간격


def test_ack_before_restore_is_ignored() -> None:
    sb = SB(commands=[{"id": "p-off", "device_id": DEV, "action": "fan_off", "source": "auto",
                       "status": "pending", "result": None}])
    st = store.AutoDeviceState()
    ctx = Ctx(sb=sb, device_uuid=DEV, meta=None, now_mono=1000.0, now_wall=T0)
    store.handle_ack(ctx, st, {"id": "m", "device_id": DEV, "action": "fan_on", "source": "manual",
                               "status": "acked", "result": "ok"})
    assert sb.calls == []                                      # 이 브리지에서 자동 경로를 안 돈 기기


# ---------- 상태 쓰기(R12) ----------

def test_state_write_only_on_change_and_retry() -> None:
    sb = SB()
    rig = Rig(sb)
    rig.run(30)                                                # 10샘플
    assert sb.count("devices", "update") == 1

    sb.fail[("devices", "update")] = 1
    rig.ack({"id": "m-1", "device_id": DEV, "action": "fan_off", "source": "manual",
             "status": "acked", "result": "ok"})               # paused_until 이 생김 → 쓰기 실패
    assert sb.count("devices", "update") == 2
    paused = rig.state()["vent"]["paused_until"]
    assert _state_row(sb)["vent"]["paused_until"] is None
    rig.run(57)
    assert sb.count("devices", "update") == 2                  # 60초 안엔 재시도 안 함
    rig.run(6)
    assert sb.count("devices", "update") == 3
    assert _state_row(sb)["vent"]["paused_until"] == paused
    rig.run(30)
    assert sb.count("devices", "update") == 3


def test_restore_primes_state_from_db() -> None:
    """DB 에 이미 같은 내용이 있으면 재시작 직후 다시 쓰지 않는다."""
    sb = SB()
    rig = Rig(sb)
    rig.run(9)
    assert sb.count("devices", "update") == 1
    sb.calls.clear()
    rig2 = Rig(sb)
    rig2.t = rig.t
    rig2.run(9)
    assert sb.count("devices", "update") == 0


# ---------- 토글 끔(R10) ----------

def test_toggle_off_sends_fan_off() -> None:
    sb = SB()
    rig = Rig(sb)
    rig.run(183, humid=85.0)
    rig.ack(acked(cmds(sb)[0]))
    rig.run(30, humid=85.0, fan="ON")
    rig.toggles = (False, False)
    rig.run(3, humid=85.0, fan="ON")
    assert [(r["action"], r["reason"]) for r in cmds(sb)] == [
        ("fan_on", "auto_vent:humid_high"), ("fan_off", "auto_vent_disabled")]
    assert _state_row(sb)["vent"]["active"] is False
    sb.calls.clear()
    rig.run(300, humid=85.0, fan="OFF")
    assert sb.calls == []                                      # 소유 없음 + 토글 꺼짐 → DB 0회
    assert [r["action"] for r in cmds(sb)] == ["fan_on", "fan_off"]
