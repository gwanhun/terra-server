"""alerts — v4 실시간 판정 경로(ALERTS_V4_ENABLED) + legacy 회귀 테스트.

계획: specs/stage-n-env-alerts-v4.md Task 2. 판정 규칙 자체는 tests/test_env_rules.py 가 보고,
여기서는 설정 로드·재시작 복원·DB 기록·푸시 적재·실패 재시도만 본다.

시계는 `alerts._now_mono` / `alerts._now_wall` 을 Clock 으로 바꿔 끼운다(mono = 1000 + t,
wall = WALL0 + t). DB 는 FakeSB 에 실패 주입·push_outbox UNIQUE 흉내를 얹은 FlakySB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend import alerts, push_events
from backend.mqtt import handlers
from tests.fake_sb import FakeSB

DEV = "11111111-1111-1111-1111-aaaaaaaaaaaa"
DEV_TEXT = "terra-test01"
OWNER = "99999999-9999-9999-9999-999999999999"
WALL0 = datetime(2026, 11, 2, 5, 0, 0, tzinfo=timezone.utc)
ENV_KINDS = {"env_temp_high", "env_temp_low", "env_humid_low", "env_humid_high"}


# ---------- 가짜 DB / 시계 ----------


class FlakySB(FakeSB):
    """FakeSB + 실패 주입(`fail[(table, op)] = 남은 횟수`) + push_outbox.event_id UNIQUE."""

    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        super().__init__(**tables)
        self.fail: dict[tuple[str, str], int] = {}
        self.fail_kind_in = 0   # alerts select 중 kind IN (...) 조회(복원·해제)만 실패시킬 횟수

    def table(self, name: str):  # type: ignore[override]
        q = super().table(name)
        orig = q.execute
        sb = self

        def execute():
            key = (name, q._op or "select")
            if sb.fail.get(key, 0) > 0:
                sb.fail[key] -= 1
                raise RuntimeError(f"boom {key}")
            if key == ("alerts", "select") and sb.fail_kind_in > 0 and any(
                    f[0] == "in" and f[1] == "kind" for f in q._filters):
                sb.fail_kind_in -= 1
                raise RuntimeError("boom alerts kind IN select")
            if name == "push_outbox" and q._op == "insert":
                eid = q._payload["event_id"]
                if any(r.get("event_id") == eid for r in sb.rows("push_outbox")):
                    raise RuntimeError('23505 duplicate key value violates unique constraint')
            return orig()

        q.execute = execute  # type: ignore[method-assign]
        return q


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return 1000.0 + self.t

    def wall(self) -> datetime:
        return WALL0 + timedelta(seconds=self.t)


def at(t: float) -> str:
    return (WALL0 + timedelta(seconds=t)).isoformat()


def _device_row(**over: Any) -> dict[str, Any]:
    row = {"id": DEV, "device_id": DEV_TEXT, "owner_id": OWNER, "name": "거실 사육장",
           "enclosure_id": None, "unlinked_at": None}
    row.update(over)
    return row


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(alerts, "_now_mono", c.mono)
    monkeypatch.setattr(alerts, "_now_wall", c.wall)
    return c


@pytest.fixture
def sb(monkeypatch: pytest.MonkeyPatch) -> FlakySB:
    s = FlakySB(devices=[_device_row()], alerts=[], push_outbox=[], device_settings=[])
    for mod in (alerts, push_events, handlers):
        monkeypatch.setattr(mod, "get_supabase_client", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALERTS_V4_ENABLED", "true")
    monkeypatch.setenv("PUSH_ENV_ALERTS_ENABLED", "true")
    monkeypatch.setenv("PUSH_EVENT_INGEST_URL", "https://example.test/functions/v1/ingest")
    monkeypatch.setenv("PUSH_EVENT_INGEST_SECRET", "s3cr3t")
    alerts.reset_state()
    alerts.reset_settings_cache()
    handlers.reset_device_cache()
    yield
    alerts.reset_state()
    alerts.reset_settings_cache()
    handlers.reset_device_cache()


def tele(t_a: float | None = None, h_a: float | None = 50.0, a_ok: bool = True) -> dict[str, Any]:
    return {"device_id": DEV, "t_a": t_a, "h_a": h_a, "a_ok": a_ok}


def run(clock: Clock, start: float, end: float, t_a: float | None, h_a: float | None = 50.0,
        a_ok: bool = True, step: float = 3.0) -> None:
    """[start, end) 를 3초 간격으로 evaluate_telemetry."""
    t = start
    while t < end:
        clock.t = t
        alerts.evaluate_telemetry(DEV, tele(t_a, h_a, a_ok), device_key=DEV_TEXT)
        t += step


def env_rows(sb: FakeSB) -> list[dict[str, Any]]:
    return [r for r in sb.rows("alerts") if r["kind"] in ENV_KINDS]


def _active_v4(kind: str = "env_temp_high", **ctx: Any) -> dict[str, Any]:
    context = {"rule": "v4", "level": "caution", "value": 26.5, "threshold": 26.0,
               "diff": 0.5, "peak_value": 26.5}
    context.update(ctx)
    return {"id": "a-old", "device_id": DEV, "kind": kind, "severity": "warning",
            "message": "x", "triggered_at": at(-7200), "resolved_at": None, "context": context}


# ---------- 플래그 꺼짐: legacy 그대로 ----------


def test_flag_off_legacy_path(monkeypatch: pytest.MonkeyPatch, sb: FlakySB, clock: Clock) -> None:
    monkeypatch.setenv("ALERTS_V4_ENABLED", "false")
    sb.rows("device_settings").append({"device_id": DEV, "alert_temp_high": 25.0})
    alerts.evaluate_telemetry(DEV, tele(26.0), device_key=DEV_TEXT)
    kinds = [r["kind"] for r in sb.rows("alerts")]
    assert kinds == ["temp_high"]
    assert env_rows(sb) == []
    assert sb.rows("push_outbox") == []


# ---------- 설정(D8) ----------


def test_defaults_when_no_settings_row(sb: FlakySB, clock: Clock) -> None:
    run(clock, 0, 123, 26.3)
    rows = env_rows(sb)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "env_temp_high" and r["severity"] == "warning"
    assert r["triggered_at"] == at(0)
    assert r["message"] == "[고온 주의] 적정 온도보다 +0.3℃"
    assert r["context"]["rule"] == "v4" and r["context"]["threshold"] == 26.0
    assert r["context"]["level"] == "caution" and r["context"]["diff"] == 0.3


def test_user_threshold_used(sb: FlakySB, clock: Clock) -> None:
    sb.rows("device_settings").append({"device_id": DEV, "target_temp_max": 28.0})
    run(clock, 0, 600, 27.0)
    assert env_rows(sb) == []


def test_settings_out_of_range_clamped(sb: FlakySB, clock: Clock) -> None:
    sb.rows("device_settings").append({"device_id": DEV, "target_temp_max": 35.0})
    run(clock, 0, 300, 29.5)
    rows = env_rows(sb)
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical" and rows[0]["context"]["level"] == "danger"
    assert rows[0]["context"]["threshold"] == 29.0


def test_settings_ttl_refresh(sb: FlakySB, clock: Clock) -> None:
    sb.rows("device_settings").append({"device_id": DEV, "target_temp_max": 26.0})
    run(clock, 0, 200, 26.3)
    assert len(env_rows(sb)) == 1
    sb.rows("device_settings")[0]["target_temp_max"] = 28.0
    run(clock, 200, 200 + 61 + 300 + 3, 27.0)
    r = env_rows(sb)[0]
    assert r["resolved_at"] is not None
    # 새 기준은 60초 안에 적용되고(타이머 초기화), 적정 시작 = 그 첫 샘플
    assert at(200) <= r["resolved_at"] <= at(261)
    assert len(env_rows(sb)) == 1


def test_settings_fetch_failure_keeps_last(sb: FlakySB, clock: Clock) -> None:
    sb.rows("device_settings").append({"device_id": DEV, "target_temp_max": 28.0})
    run(clock, 0, 3, 27.0)
    sb.fail[("device_settings", "select")] = 10_000
    run(clock, 3, 600, 27.0)   # 디폴트(26)로 떨어졌다면 열렸을 것
    assert env_rows(sb) == []


def test_settings_first_fetch_failure_defers(sb: FlakySB, clock: Clock) -> None:
    sb.fail[("device_settings", "select")] = 1
    run(clock, 0, 3, 26.3)     # 직전 값 없음 → 보류(예외 없이), 30초 뒤 재조회
    run(clock, 3, 153, 26.3)
    rows = env_rows(sb)
    assert len(rows) == 1 and rows[0]["triggered_at"] == at(30)


def test_settings_fetch_failure_backs_off(sb: FlakySB, clock: Clock) -> None:
    # 조회 실패 중엔 샘플마다(3초) 다시 묻지 않고 30초에 한 번만
    sb.fail[("device_settings", "select")] = 10_000
    run(clock, 0, 60, 26.3)
    assert 10_000 - sb.fail[("device_settings", "select")] == 2


# ---------- 재시작 복원 ----------


def test_restore_no_duplicate_push(sb: FlakySB, clock: Clock) -> None:
    sb.rows("alerts").append(_active_v4(pushed_at=at(-5400), reminded_at=at(-1800)))
    run(clock, 0, 7200, 26.5, step=6.0)
    assert len(sb.rows("alerts")) == 1
    assert sb.rows("alerts")[0]["resolved_at"] is None
    assert sb.rows("push_outbox") == []


def test_restore_reminder_due_once(sb: FlakySB, clock: Clock) -> None:
    sb.rows("alerts").append(_active_v4(pushed_at=at(-3500)))
    run(clock, 0, 300, 26.5)
    ids = [r["event_id"] for r in sb.rows("push_outbox")]
    assert ids == ["alert:a-old:reminder"]
    # 푸시 후 3600초 = t=100 → 3초 격자의 첫 샘플 t=102
    assert sb.rows("alerts")[0]["context"]["reminded_at"] == at(102)


def test_restore_query_failure_defers(sb: FlakySB, clock: Clock) -> None:
    sb.rows("alerts").append(_active_v4(pushed_at=at(-600)))
    sb.fail_kind_in = 1
    run(clock, 0, 3, 26.5)
    assert len(sb.rows("alerts")) == 1
    run(clock, 3, 600, 26.5)
    assert len(sb.rows("alerts")) == 1
    assert sb.rows("push_outbox") == []


def test_restore_multiple_active_keeps_latest(sb: FlakySB, clock: Clock) -> None:
    old = _active_v4(peak_value=27.0)
    new = _active_v4()
    new.update(id="a-new", triggered_at=at(-600))
    sb.rows("alerts").extend([old, new])
    run(clock, 0, 3, 26.5)
    o = sb.one("alerts", id="a-old")
    n = sb.one("alerts", id="a-new")
    assert o["resolved_at"] == at(0)
    assert o["context"]["peak_diff"] == 1.0          # 강제 종료도 peak_diff 기록
    assert n["resolved_at"] is None
    assert alerts._states[DEV].trackers["temp"].active.alert_id == "a-new"


def test_restored_silent_caution_redanger_no_push(sb: FlakySB, clock: Clock) -> None:
    cau = _active_v4(deescalated_from="d-1", chain_root="d-1", value=27.5, peak_value=27.5)
    cau["triggered_at"] = at(-600)
    sb.rows("alerts").append(cau)
    run(clock, 0, 123, 29.4)
    danger = [r for r in env_rows(sb) if r["severity"] == "critical"]
    assert len(danger) == 1
    assert danger[0]["context"]["push_suppressed"] is True
    assert danger[0]["context"]["chain_root"] == "d-1"
    assert sb.one("alerts", id="a-old")["resolved_at"] == danger[0]["triggered_at"]
    assert sb.rows("push_outbox") == []


def test_legacy_bridge_rows_closed(sb: FlakySB, clock: Clock) -> None:
    sb.rows("alerts").append({"id": "l-1", "device_id": DEV, "kind": "temp_high", "severity": "warning",
                              "triggered_at": at(-9000), "resolved_at": None,
                              "context": {"t_a": 27.0, "threshold": 26.0}})
    run(clock, 0, 3, 24.0)
    assert sb.one("alerts", id="l-1")["resolved_at"] == at(0)


def test_firmware_alert_untouched(sb: FlakySB, clock: Clock) -> None:
    sb.rows("alerts").extend([
        {"id": "f-1", "device_id": DEV, "kind": "temp_high", "severity": "critical",
         "triggered_at": at(-9000), "resolved_at": None, "context": {"t": 31.0}},
        # 펌웨어가 threshold 를 실어 보내도 구 브리지 행(정확히 {t_a|h_a, threshold})이 아니면 그대로
        {"id": "f-3", "device_id": DEV, "kind": "temp_high", "severity": "critical",
         "triggered_at": at(-9000), "resolved_at": None, "context": {"t": 31.0, "threshold": 30.0}},
        {"id": "f-2", "device_id": DEV, "kind": "heater_latched", "severity": "critical",
         "triggered_at": at(-9000), "resolved_at": None, "context": None},
    ])
    run(clock, 0, 600, 24.0)
    assert sb.one("alerts", id="f-1")["resolved_at"] is None
    assert sb.one("alerts", id="f-2")["resolved_at"] is None
    assert sb.one("alerts", id="f-3")["resolved_at"] is None


# ---------- 기록 실패·푸시 ----------


def test_open_insert_failure_rolls_back(sb: FlakySB, clock: Clock) -> None:
    run(clock, 0, 120, 26.3)
    sb.fail[("alerts", "insert")] = 1
    run(clock, 120, 123, 26.3)
    assert env_rows(sb) == []
    run(clock, 123, 126, 26.3)
    rows = env_rows(sb)
    assert len(rows) == 1 and rows[0]["triggered_at"] == at(0)
    ids = [r["event_id"] for r in sb.rows("push_outbox")]
    assert ids == [f"alert:{rows[0]['id']}:started"]
    assert rows[0]["context"]["pushed_at"] == at(123)


def test_push_disabled_no_pushed_at(monkeypatch: pytest.MonkeyPatch, sb: FlakySB, clock: Clock) -> None:
    monkeypatch.setenv("PUSH_ENV_ALERTS_ENABLED", "false")
    run(clock, 0, 3900, 26.3, step=6.0)
    rows = env_rows(sb)
    assert len(rows) == 1
    assert "pushed_at" not in rows[0]["context"]
    assert "reminded_at" not in rows[0]["context"]
    assert sb.rows("push_outbox") == []


def test_push_failed_retries_10min(sb: FlakySB, clock: Clock) -> None:
    sb.fail[("push_outbox", "insert")] = 2
    run(clock, 0, 126, 26.3)          # t=120 실패, t=123 재시도 실패
    r = env_rows(sb)[0]
    assert "pushed_at" not in r["context"] and sb.rows("push_outbox") == []
    run(clock, 126, 129, 26.3)        # t=126 재시도 성공
    assert r["context"]["pushed_at"] == at(126)
    assert [x["event_id"] for x in sb.rows("push_outbox")] == [f"alert:{r['id']}:started"]


def test_push_failed_gives_up_after_10min(sb: FlakySB, clock: Clock) -> None:
    sb.fail[("push_outbox", "insert")] = 10_000
    run(clock, 0, 720, 26.3)
    sb.fail.clear()
    run(clock, 720, 4000, 26.3, step=6.0)
    r = env_rows(sb)[0]
    assert "pushed_at" not in r["context"]
    assert sb.rows("push_outbox") == []     # 포기했으니 started·reminder 모두 없음


def test_reminder_failed_retries(sb: FlakySB, clock: Clock) -> None:
    run(clock, 0, 123, 26.3)                 # t=120 open + started 적재
    r = env_rows(sb)[0]
    sb.fail[("push_outbox", "insert")] = 1
    run(clock, 123, 120 + 3600 + 6, 26.3)    # t=3720 reminder 실패 → t=3723 재시도 성공
    ids = [x["event_id"] for x in sb.rows("push_outbox")]
    assert ids == [f"alert:{r['id']}:started", f"alert:{r['id']}:reminder"]
    assert r["context"]["reminded_at"] == at(3723)


def test_caution_to_danger_rows(sb: FlakySB, clock: Clock) -> None:
    run(clock, 0, 200, 26.3)
    run(clock, 200, 330, 29.4)
    rows = env_rows(sb)
    cau = [r for r in rows if r["severity"] == "warning"][0]
    dan = [r for r in rows if r["severity"] == "critical"][0]
    assert cau["resolved_at"] == dan["triggered_at"] == at(200)
    assert dan["message"] == "[고온 경고] 온도가 너무 높아 개체가 위험할 수 있어요"
    assert dan["context"]["threshold"] == 26.0
    assert len(sb.rows("push_outbox")) == 2


def test_deescalation_insert_failure_no_push(sb: FlakySB, clock: Clock) -> None:
    dan = _active_v4(level="danger", value=29.5, peak_value=29.5, pushed_at=at(-600))
    dan.update(id="d-1", severity="critical", triggered_at=at(-900))
    sb.rows("alerts").append(dan)
    run(clock, 0, 300, 27.5)
    sb.fail[("alerts", "insert")] = 1
    run(clock, 300, 303, 27.5)               # 완화 전이 — 주의 INSERT 실패
    assert sb.one("alerts", id="d-1")["resolved_at"] is None   # 경고 종료도 커밋 안 됨
    assert len(sb.rows("alerts")) == 1
    run(clock, 303, 306, 27.5)               # 같은 전이를 다시 — 이번엔 무음 주의로
    assert sb.one("alerts", id="d-1")["resolved_at"] == at(0)
    cau = [r for r in env_rows(sb) if r["id"] != "d-1"]
    assert len(cau) == 1
    assert cau[0]["context"]["deescalated_from"] == "d-1"
    assert cau[0]["context"]["chain_root"] == "d-1"
    assert cau[0]["triggered_at"] == at(0)
    assert sb.rows("push_outbox") == []


def test_close_writes_times_and_peak(sb: FlakySB, clock: Clock) -> None:
    run(clock, 0, 60, 26.3)
    run(clock, 60, 1000, 27.8)
    run(clock, 1000, 1303, 25.0)
    r = env_rows(sb)[0]
    assert r["resolved_at"] == at(1000)
    assert r["context"]["peak_value"] == 27.8
    assert r["context"]["peak_diff"] == 1.8


@pytest.mark.parametrize("flag", ["true", "false"])
def test_sensor_fault_regression(monkeypatch: pytest.MonkeyPatch, sb: FlakySB, clock: Clock,
                                 flag: str) -> None:
    monkeypatch.setenv("ALERTS_V4_ENABLED", flag)
    sb.rows("device_settings").append({"device_id": DEV, "alert_temp_high": 30.0})
    run(clock, 0, 9, None, None, a_ok=False)
    faults = [r for r in sb.rows("alerts") if r["kind"] == "sensor_fault"]
    assert len(faults) == 1 and faults[0].get("resolved_at") is None
    run(clock, 9, 12, 24.0)
    assert faults[0].get("resolved_at") is not None


def test_unlinked_device_stops(sb: FlakySB, clock: Clock) -> None:
    sb.rows("devices")[0]["unlinked_at"] = at(-60)
    sb.rows("alerts").append(_active_v4(peak_value=27.0))
    run(clock, 0, 3, 26.5)
    a = sb.one("alerts", id="a-old")
    assert a["resolved_at"] == at(0) and a["context"]["peak_diff"] == 1.0
    run(clock, 3, 300, 26.5)
    assert len(sb.rows("alerts")) == 1
    assert sb.rows("push_outbox") == []


def test_unlink_seen_fresh_before_open_despite_stale_cache(sb: FlakySB, clock: Clock) -> None:
    # 해제 API 는 API 프로세스라 브리지 device_meta 캐시(5분, 실제 monotonic)를 못 비운다.
    # 해제 뒤 telemetry 가 계속 와도 새 알림을 열기 직전 fresh 조회로 해제를 알아채야 한다(D12 보강).
    run(clock, 0, 3, 24.0)                        # 캐시에 "연결됨" 메타 적재
    sb.rows("devices")[0]["unlinked_at"] = at(3)  # 브리지 캐시는 그대로
    run(clock, 3, 300, 26.5)                      # 2분 넘게 범위 밖 — 열리면 안 됨
    assert env_rows(sb) == []
    assert sb.rows("push_outbox") == []
    assert handlers.device_meta(DEV)["unlinked_at"] == at(3)   # 캐시가 비워져 fresh 값


def test_unlink_seen_fresh_before_reminder(sb: FlakySB, clock: Clock) -> None:
    # 열린 알림이 있는 채로 해제(캐시는 stale) → 1시간 뒤 reminder 푸시가 나가면 안 된다
    run(clock, 0, 126, 26.3)
    opened = env_rows(sb)
    assert len(opened) == 1 and len(sb.rows("push_outbox")) == 1
    sb.rows("devices")[0]["unlinked_at"] = at(200)
    run(clock, 200, 3900, 26.3)
    assert len(sb.rows("push_outbox")) == 1                     # started 만, reminder 없음
    assert env_rows(sb)[0]["resolved_at"] is not None           # _stop_unlinked 가 닫음


def test_relinked_device_resumes_and_unlink_closes_again(sb: FlakySB, clock: Clock) -> None:
    dev = sb.rows("devices")[0]
    dev["unlinked_at"] = at(-60)
    run(clock, 0, 3, 26.5)                       # 해제 확인
    dev["unlinked_at"] = None                    # 같은 행 재페어링
    handlers.reset_device_cache()
    run(clock, 3, 126, 26.3)                     # 새 이벤트
    opened = env_rows(sb)
    assert len(opened) == 1 and opened[0].get("resolved_at") is None
    dev["unlinked_at"] = at(130)                 # 다시 해제
    handlers.reset_device_cache()
    run(clock, 132, 135, 26.3)
    assert env_rows(sb)[0]["resolved_at"] == at(132)


def test_unlink_api_closes_active_v4_alerts(monkeypatch: pytest.MonkeyPatch, sb: FlakySB) -> None:
    # 해제 뒤엔 telemetry 가 안 와서 브리지가 못 닫는다 → 해제 API 가 닫는다(D12)
    from backend import unlink_service
    monkeypatch.setattr(unlink_service.registry, "unregister_device", lambda key: None)
    sb.rows("alerts").append(_active_v4(peak_value=27.0))
    sb.rows("alerts").append({"id": "f-1", "device_id": DEV, "kind": "heater_latched",
                              "severity": "critical", "triggered_at": at(-9000),
                              "resolved_at": None, "context": None})
    unlink_service.unlink_entity(sb, table="devices", entity_uuid=DEV, user_id=OWNER,
                                 request_id="req-1")
    a = sb.one("alerts", id="a-old")
    assert a["resolved_at"] is not None and a["context"]["peak_diff"] == 1.0
    assert sb.one("alerts", id="f-1")["resolved_at"] is None


def test_sensor_fault_not_queried_every_sample(monkeypatch: pytest.MonkeyPatch, sb: FlakySB,
                                               clock: Clock) -> None:
    calls: list[str] = []
    orig = alerts._active_alert

    def counting(sb_: Any, device_uuid: str, kind: str):
        if kind == "sensor_fault":
            calls.append(kind)
        return orig(sb_, device_uuid, kind)

    monkeypatch.setattr(alerts, "_active_alert", counting)
    run(clock, 0, 300, 24.0)                     # 정상 100샘플
    assert len(calls) == 1
    run(clock, 300, 309, None, None, a_ok=False)
    run(clock, 309, 400, 24.0)
    faults = [r for r in sb.rows("alerts") if r["kind"] == "sensor_fault"]
    assert len(faults) == 1 and faults[0].get("resolved_at") is not None
    assert len(calls) <= 3                       # 상태가 바뀔 때만


def test_redanger_chain_reminder_rows(sb: FlakySB, clock: Clock) -> None:
    # 복원한 무음 주의(체인 최초 푸시 40분 전) → 재경고(푸시 생략) → 최초 푸시 +1시간에 reminder 1회
    cau = _active_v4(deescalated_from="d-1", chain_root="d-1", value=27.5, peak_value=27.5,
                     chain_pushed_at=at(-2400), chain_reminded=False)
    cau["triggered_at"] = at(-600)
    sb.rows("alerts").append(cau)
    run(clock, 0, 1500, 29.4)
    danger = [r for r in env_rows(sb) if r["severity"] == "critical"]
    assert len(danger) == 1 and danger[0]["context"]["push_suppressed"] is True
    assert danger[0]["context"]["chain_pushed_at"] == at(-2400)
    assert [r["event_id"] for r in sb.rows("push_outbox")] == [f"alert:{danger[0]['id']}:reminder"]
    assert danger[0]["context"]["reminded_at"] == at(1200)


def test_handler_to_outbox_integration(sb: FlakySB, clock: Clock) -> None:
    for i in range(41):
        clock.t = i * 3.0
        handlers.handle_telemetry(DEV_TEXT, {
            "ts": 1_793_600_000 + i * 3,
            "dht22_a": {"t": 26.3, "h": 55.0, "ok": True},
            "dht22_b": {"t": 26.0, "h": 55.0, "ok": True},
        })
    rows = env_rows(sb)
    assert len(rows) == 1 and rows[0]["kind"] == "env_temp_high"
    out = sb.rows("push_outbox")
    assert len(out) == 1
    p = out[0]["body"]["payload"]
    assert p["device_key"] == DEV_TEXT and p["level"] == "caution" and p["phase"] == "started"
    assert out[0]["body"]["user_id"] == OWNER


# ---------- scripts/close_v4_alerts.py ----------


def test_close_v4_alerts_script_dry_run_then_apply(sb: FlakySB) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "close_v4_alerts.py"
    spec = importlib.util.spec_from_file_location("close_v4_alerts", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sb.rows("alerts").extend([
        _active_v4(peak_value=27.0),
        {"id": "f-1", "device_id": DEV, "kind": "temp_high", "resolved_at": None, "context": {"t": 31}},
        {"id": "o-1", "device_id": DEV, "kind": "env_vent_delay", "resolved_at": None,
         "context": {"rule": "v4"}},
        {"id": "x-1", "device_id": DEV, "kind": "env_temp_low", "resolved_at": None, "context": {}},
    ])
    lines: list[str] = []
    assert mod.run(sb, apply=False, out=lines.append) == 1
    assert all(r.get("resolved_at") is None for r in sb.rows("alerts"))
    assert any("a-old" in ln for ln in lines)

    assert mod.run(sb, apply=True, now=WALL0, out=lines.append) == 1
    a = sb.one("alerts", id="a-old")
    assert a["resolved_at"] == at(0) and a["context"]["peak_diff"] == 1.0
    for rid in ("f-1", "o-1", "x-1"):
        assert sb.one("alerts", id=rid)["resolved_at"] is None
