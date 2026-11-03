"""humid_cycle_store — Stage O 습기 사이클 DB·푸시 연결 (v4 경로 통합).

계획: specs/stage-o-humid-cycle-v4.md Task 4. 판정 규칙은 tests/test_humid_cycle.py 가 보고,
여기서는 humid_cycles 기록·환기 지연/분무 공백 알림·푸시·복원·해제·실패 재시도를 본다.
시계·가짜 DB 는 tests/test_alerts.py 의 Clock·FlakySB 를 그대로 쓴다(wall = WALL0 + t, 샘플 ts 도 같은 축).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend import alerts, push_events
from backend.mqtt import handlers
from tests.test_alerts import DEV, DEV_TEXT, OWNER, WALL0, Clock, FlakySB, _device_row

VENT = "[환기 지연] 분무 후 건기 미도달"
MIST = "[건조 주의] 분무 필요"


def at(t: float) -> str:
    return (WALL0 + timedelta(seconds=t)).isoformat()


class LossySB(FlakySB):
    """`lose[(table, op)] = n` — 쓰기는 반영되는데 응답이 유실된 것처럼 예외."""

    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        super().__init__(**tables)
        self.lose: dict[tuple[str, str], int] = {}

    def table(self, name: str):  # type: ignore[override]
        q = super().table(name)
        inner = q.execute
        sb = self

        def execute():
            res = inner()
            key = (name, q._op or "select")
            if sb.lose.get(key, 0) > 0:
                sb.lose[key] -= 1
                raise RuntimeError(f"lost response {key}")
            return res

        q.execute = execute  # type: ignore[method-assign]
        return q


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(alerts, "_now_mono", c.mono)
    monkeypatch.setattr(alerts, "_now_wall", c.wall)
    return c


@pytest.fixture
def sb(monkeypatch: pytest.MonkeyPatch) -> LossySB:
    s = LossySB(devices=[_device_row()], alerts=[], push_outbox=[], device_settings=[],
                humid_cycles=[], telemetry=[])
    for mod in (alerts, push_events, handlers):
        monkeypatch.setattr(mod, "get_supabase_client", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALERTS_V4_ENABLED", "true")
    monkeypatch.setenv("PUSH_ENV_ALERTS_ENABLED", "true")
    monkeypatch.setenv("PUSH_EVENT_INGEST_URL", "https://example.test/functions/v1/ingest")
    monkeypatch.setenv("PUSH_EVENT_INGEST_SECRET", "s3cr3t")
    monkeypatch.setenv("HUMID_CYCLES_ENABLED", "true")
    monkeypatch.setenv("HUMID_CYCLES_SINCE", at(-200000))
    alerts.reset_state()
    alerts.reset_settings_cache()
    handlers.reset_device_cache()
    yield
    alerts.reset_state()
    alerts.reset_settings_cache()
    handlers.reset_device_cache()


def sample(clock: Clock, t: float, h: float | None, *, ts: float | None = None, a_ok: bool = True) -> None:
    clock.t = t
    row = {"device_id": DEV, "ts": at(t if ts is None else ts), "t_a": 24.0, "h_a": h, "a_ok": a_ok}
    alerts.evaluate_telemetry(DEV, row, device_key=DEV_TEXT)


def run(clock: Clock, start: float, end: float, h0: float, h1: float | None = None, step: float = 3.0) -> None:
    """[start, end) 3초 간격, 습도는 h0 → h1 직선."""
    h1 = h0 if h1 is None else h1
    t = start
    while t < end:
        sample(clock, t, h0 + (h1 - h0) * (t - start) / (end - start))
        t += step


def rise(clock: Clock, t0: float = 0.0) -> None:
    """t0 부터 10분 50% → 1분 만에 70% → 1분 유지(감지까지)."""
    run(clock, t0, t0 + 600, 50)
    run(clock, t0 + 600, t0 + 660, 50, 70)
    run(clock, t0 + 660, t0 + 720, 70)


def cycle_row(**over: Any) -> dict[str, Any]:
    """DB 에 이미 있는 사이클. 기본: MAX 70 확정(건기 목표 58), 미도달, 시작 1시간 전."""
    row = {
        "id": "cyc-1", "device_id": DEV, "started_at": at(-3600), "start_humid": 50.0,
        "detected_at": at(-3560), "max_until": at(-3380), "max_raw": 70.0, "max_humid": 70.0,
        "dry_target": 58.0, "dry_reached_at": None, "status": "open", "closed_at": None,
        "ended_by": None, "offline_sec": 0, "merged_count": 0,
        "vent_delay_alert_id": None, "mist_gap_alert_id": None, "updated_at": at(-3),
    }
    row.update(over)
    return row


def cycle_alert(kind: str, alert_id: str = "al-1", triggered: float = -3600, **ctx: Any) -> dict[str, Any]:
    context = {"rule": "v4", "level": "caution", "cycle_id": "cyc-1", "value": 65.0,
               "pushed_at": at(triggered)}
    context.update(ctx)
    return {"id": alert_id, "device_id": DEV, "kind": kind, "severity": "warning",
            "message": VENT if kind == "env_vent_delay" else MIST,
            "triggered_at": at(triggered), "resolved_at": None, "context": context}


def kind_rows(sb: FlakySB, kind: str) -> list[dict[str, Any]]:
    return [r for r in sb.rows("alerts") if r["kind"] == kind]


# ---------- 기록 ----------

def test_open_upserts_cycle_row(sb: LossySB, clock: Clock) -> None:
    rise(clock)
    rows = sb.rows("humid_cycles")
    assert len(rows) == 1
    r = rows[0]
    assert r["device_id"] == DEV and r["status"] == "open" and r["start_humid"] == 50.0
    assert r["offline_sec"] == 0 and r["merged_count"] == 0 and r["updated_at"]
    assert at(600) <= r["started_at"] <= at(630) and r["ended_by"] is None


def test_open_fail_retries_same_started_at_then_closes(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(
        started_at=at(-1000), detected_at=at(-990), max_until=at(-810), start_humid=40.0,
        max_raw=52.0, max_humid=52.0, dry_target=40.0))
    sb.fail[("humid_cycles", "upsert")] = 1
    rise(clock)
    rows = sorted(sb.rows("humid_cycles"), key=lambda r: r["started_at"])
    assert len(rows) == 2
    prev, new = rows
    assert prev["status"] == "not_reached" and prev["ended_by"] == "next_event"
    assert prev["closed_at"] == new["started_at"]


# ---------- 환기 지연 ----------

def test_vent_delay_alert_and_push_once(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-21540)))
    run(clock, 0, 120, 65)
    vents = kind_rows(sb, "env_vent_delay")
    assert len(vents) == 1
    a = vents[0]
    assert a["severity"] == "warning" and a["message"] == VENT and a["triggered_at"] == at(60)
    assert a["context"]["rule"] == "v4" and a["context"]["level"] == "caution"
    assert a["context"]["cycle_id"] == "cyc-1" and a["context"]["cycle_started_at"] == at(-21540)
    assert a["context"].get("pushed_at")
    out = sb.rows("push_outbox")
    assert [o["event_id"] for o in out] == [f"alert:{a['id']}:started"]
    assert out[0]["body"]["payload"]["kind"] == "env_vent_delay"
    assert out[0]["body"]["payload"]["title"] == VENT
    assert sb.rows("humid_cycles")[0]["vent_delay_alert_id"] == a["id"]
    run(clock, 120, 600, 65)
    assert len(sb.rows("push_outbox")) == 1


def test_alert_insert_lost_response_no_duplicate(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-21540)))
    sb.lose[("alerts", "insert")] = 1
    run(clock, 0, 120, 65)
    assert len(kind_rows(sb, "env_vent_delay")) == 1
    assert len(sb.rows("push_outbox")) == 1


def test_dry_reached_resolves_vent_delay(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-25200), vent_delay_alert_id="al-1"))
    sb.rows("alerts").append(cycle_alert("env_vent_delay", triggered=-3600))
    run(clock, 0, 60, 65, 55)
    run(clock, 60, 120, 55)
    c = sb.rows("humid_cycles")[0]
    a = sb.one("alerts", id="al-1")
    assert c["status"] == "reached_late" and c["dry_reached_at"] is not None
    assert a["resolved_at"] == c["dry_reached_at"] and a["context"]["closed_reason"] == "dry"


def test_restore_keeps_timers_and_no_double_open(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-25200), vent_delay_alert_id="al-1"))
    sb.rows("alerts").append(cycle_alert("env_vent_delay", triggered=-3600))
    run(clock, 0, 300, 65)
    assert len(kind_rows(sb, "env_vent_delay")) == 1
    assert sb.rows("push_outbox") == []


def test_restore_closes_leftover_vent_delay(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(
        started_at=at(-30000), status="reached_late", dry_reached_at=at(-600), vent_delay_alert_id="al-1"))
    sb.rows("alerts").append(cycle_alert("env_vent_delay", triggered=-8400))
    sample(clock, 0, 50)
    a = sb.one("alerts", id="al-1")
    assert a["resolved_at"] == at(-600)


def test_restart_gap_from_prev_valid_row(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-3600), updated_at=at(-1800)))
    sb.rows("telemetry").extend([
        {"device_id": DEV, "ts": at(-1800), "h_a": 66.0, "a_ok": True},
        {"device_id": DEV, "ts": at(-60), "h_a": None, "a_ok": False},
    ])
    run(clock, 0, 30, 65)
    assert sb.rows("humid_cycles")[0]["offline_sec"] == 1800


# ---------- 분무 공백 ----------

def test_mist_gap_alert_and_close_on_new_event(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(
        started_at=at(-53940), status="reached_in_time", dry_reached_at=at(-40000)))
    run(clock, 0, 120, 50)
    mists = kind_rows(sb, "env_mist_gap")
    assert len(mists) == 1 and mists[0]["message"] == MIST and mists[0]["triggered_at"] == at(60)
    assert len(sb.rows("push_outbox")) == 1
    rise(clock, 120)
    new = max(sb.rows("humid_cycles"), key=lambda r: r["started_at"])
    m = kind_rows(sb, "env_mist_gap")[0]
    assert m["resolved_at"] == max(new["started_at"], m["triggered_at"])


# ---------- 복원 범위 ----------

def test_reenable_no_storm(monkeypatch: pytest.MonkeyPatch, sb: LossySB, clock: Clock) -> None:
    monkeypatch.setenv("HUMID_CYCLES_SINCE", at(-3600))
    sb.rows("humid_cycles").extend([
        cycle_row(id="cyc-old", started_at=at(-90000), status="reached_in_time",
                  dry_reached_at=at(-80000)),                                   # 켠 시각 이전
        cycle_row(id="cyc-dis", started_at=at(-60000), status="reached_in_time",
                  dry_reached_at=at(-50000), ended_by="disabled"),
    ])
    run(clock, 0, 120, 50)
    assert sb.rows("alerts") == [] and sb.rows("push_outbox") == []


def test_repair_starts_fresh(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(
        started_at=at(-20000), status="reached_in_time", dry_reached_at=at(-15000)))
    dev = sb.rows("devices")[0]
    dev["unlinked_at"] = at(-60)
    sample(clock, 0, 50)
    assert sb.rows("humid_cycles")[0]["ended_by"] == "unlinked"
    dev["unlinked_at"] = None
    handlers.reset_device_cache()
    run(clock, 40000, 40120, 50)
    assert kind_rows(sb, "env_mist_gap") == []


def test_burst_uses_row_ts(sb: LossySB, clock: Clock) -> None:
    # 재연결 직후 몰려 온 샘플: wall 은 1000초에 멈춰 있고 row.ts 만 3초씩 간다
    def burst(ts0: float, ts1: float, h0: float, h1: float) -> None:
        ts = ts0
        while ts < ts1:
            sample(clock, 1000, h0 + (h1 - h0) * (ts - ts0) / (ts1 - ts0), ts=ts)
            ts += 3
    burst(400, 700, 50, 50)
    burst(700, 760, 50, 70)
    burst(760, 820, 70, 70)
    rows = sb.rows("humid_cycles")
    assert len(rows) == 1 and at(700) <= rows[0]["started_at"] <= at(730)


# ---------- 푸시 ----------

def test_push_retry_confirms_linked(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-21540)))
    sb.fail[("push_outbox", "insert")] = 1
    run(clock, 0, 63, 65)                      # 60초에 환기 지연, 푸시 실패
    assert len(kind_rows(sb, "env_vent_delay")) == 1 and sb.rows("push_outbox") == []
    sb.rows("devices")[0]["unlinked_at"] = at(62)
    run(clock, 63, 120, 65)
    assert sb.rows("push_outbox") == []
    assert kind_rows(sb, "env_vent_delay")[0]["resolved_at"] is not None


def test_push_disabled_records_only(monkeypatch: pytest.MonkeyPatch, sb: LossySB, clock: Clock) -> None:
    monkeypatch.setenv("PUSH_ENV_ALERTS_ENABLED", "false")
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-21540)))
    run(clock, 0, 120, 65)
    vents = kind_rows(sb, "env_vent_delay")
    assert len(vents) == 1 and not vents[0]["context"].get("pushed_at")
    assert sb.rows("push_outbox") == []


@pytest.mark.parametrize("env", [{"HUMID_CYCLES_ENABLED": "false"}, {"HUMID_CYCLES_SINCE": ""}])
def test_flag_off_or_no_since_noop(monkeypatch: pytest.MonkeyPatch, sb: LossySB, clock: Clock,
                                   env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rise(clock)
    assert sb.rows("humid_cycles") == []


# ---------- 해제 ----------

def test_unlink_closes_cycle_alerts_bridge(sb: LossySB, clock: Clock) -> None:
    sb.rows("humid_cycles").append(cycle_row(started_at=at(-25200), vent_delay_alert_id="al-1"))
    sb.rows("alerts").append(cycle_alert("env_vent_delay"))
    sb.rows("devices")[0]["unlinked_at"] = at(-60)
    sample(clock, 0, 65)
    assert sb.one("alerts", id="al-1")["resolved_at"] == at(0)
    c = sb.rows("humid_cycles")[0]
    assert c["ended_by"] == "unlinked" and c["status"] == "not_reached"


def test_unlink_api_closes_cycle_alerts(monkeypatch: pytest.MonkeyPatch, sb: LossySB) -> None:
    from backend import unlink_service
    monkeypatch.setattr(unlink_service.registry, "unregister_device", lambda key: None)
    sb.rows("humid_cycles").append(cycle_row(status="reached_in_time", dry_reached_at=at(-100),
                                             mist_gap_alert_id="al-2"))
    sb.rows("alerts").append(cycle_alert("env_mist_gap", alert_id="al-2"))
    unlink_service.unlink_entity(sb, table="devices", entity_uuid=DEV, user_id=OWNER, request_id="req-1")
    assert sb.one("alerts", id="al-2")["resolved_at"] is not None
    assert sb.rows("humid_cycles")[0]["ended_by"] == "unlinked"


def test_close_script_cycles(sb: LossySB) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "close_v4_alerts.py"
    spec = importlib.util.spec_from_file_location("close_v4_alerts", path)
    assert spec and spec.loader
    close_v4_alerts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(close_v4_alerts)
    sb.rows("humid_cycles").append(cycle_row(vent_delay_alert_id="al-1"))
    sb.rows("alerts").append(cycle_alert("env_vent_delay"))
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    assert close_v4_alerts.run(sb, apply=True, cycles=True, now=now, out=lambda s: None) == 1
    assert sb.one("alerts", id="al-1")["resolved_at"] == now.isoformat()
    assert sb.rows("humid_cycles")[0]["ended_by"] == "disabled"
