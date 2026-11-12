"""Stage P Task 3 — 일일 분석 생성 스레드(analysis_runner) 테스트.

FakeSB 로 대상 기기·입력 조회·저장을 실제로 굴린다. 기준 날짜: report_date = 2026-11-02
(구간 11/2 08:00 ~ 11/3 08:00 KST = 2026-11-01T23:00Z ~ 2026-11-02T23:00Z), tick 은 11/3 KST.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend import analysis_runner
from backend.analysis_runner import AnalysisRunner, reports_enabled
from backend.daily_analysis import KST
from tests.fake_sb import FakeSB

UTC = timezone.utc
REPORT_DATE = "2026-11-02"
WIN_START = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
WIN_END = datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
LAST_BUCKET = (WIN_END - timedelta(minutes=30)).isoformat()


def kst(h: int, m: int = 0, day: int = 3) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=KST)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def dev(i: int, **kw: Any) -> dict[str, Any]:
    return {"id": f"dev-{i:04d}", "owner_id": "user-1", "unlinked_at": None, **kw}


def alert(device_id: str, kind: str, start: datetime, end: datetime | None, *,
          level: str = "caution", rule: str | None = "v4", **ctx: Any) -> dict[str, Any]:
    context: dict[str, Any] = {"level": level, **ctx}
    if rule is not None:
        context["rule"] = rule
    return {
        "device_id": device_id, "kind": kind,
        "severity": "critical" if level == "danger" else "warning",
        "triggered_at": iso(start), "resolved_at": iso(end) if end else None,
        "context": context,
    }


class RecordingSB(FakeSB):
    """어느 테이블을 건드렸는지 기록."""

    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        super().__init__(**tables)
        self.touched: list[str] = []

    def table(self, name: str):  # type: ignore[override]
        self.touched.append(name)
        return super().table(name)


class ExplodingSB:
    def table(self, name: str):
        raise AssertionError(f"꺼져 있으면 테이블을 건드리면 안 됨: {name}")


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERTS_V4_ENABLED", "true")
    monkeypatch.setenv("ENV_REPORTS_ENABLED", "true")
    # 10/27: 일일 테스트 날짜(11/2~11/4)의 주간 대상(10/26 주)은 since 이전이라 주간이 섞이지 않는다
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-10-27T00:00:00+09:00")
    monkeypatch.delenv("HUMID_CYCLES_ENABLED", raising=False)


def report(sb: FakeSB, device_id: str = "dev-0001") -> dict[str, Any] | None:
    return sb.one("env_daily_reports", device_id=device_id, report_date=REPORT_DATE)


# ---------- 플래그 ----------

def test_disabled_flags_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERTS_V4_ENABLED", "true")
    monkeypatch.delenv("ENV_REPORTS_ENABLED", raising=False)
    assert reports_enabled() is False
    assert AnalysisRunner(sb=ExplodingSB()).tick(kst(9, 30)) == 0

    monkeypatch.setenv("ALERTS_V4_ENABLED", "false")
    monkeypatch.setenv("ENV_REPORTS_ENABLED", "yes")
    assert reports_enabled() is False
    assert AnalysisRunner(sb=ExplodingSB()).tick(kst(9, 30)) == 0

    monkeypatch.setenv("ALERTS_V4_ENABLED", "on")
    assert reports_enabled() is True


# ---------- 시각 규칙 (P3) ----------

def test_tick_before_8am_noop(enabled: None) -> None:
    sb = RecordingSB(devices=[dev(1)], telemetry_30m=[{"device_id": "dev-0001", "bucket": LAST_BUCKET}])
    assert AnalysisRunner(sb=sb).tick(kst(7, 59)) == 0
    assert sb.rows("env_daily_reports") == []
    assert sb.touched == []   # 08시 전엔 조회도 안 함


def test_tick_boundary_utc_2300_is_kst_0800(enabled: None) -> None:
    sb = FakeSB(devices=[dev(1)], telemetry_30m=[{"device_id": "dev-0001", "bucket": LAST_BUCKET}])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(datetime(2026, 11, 2, 22, 59, 59, tzinfo=UTC)) == 0
    assert runner.tick(datetime(2026, 11, 2, 23, 0, tzinfo=UTC)) == 1   # = 11/3 08:00 KST
    row = report(sb)
    assert row is not None
    assert row["pass"] == 1
    assert row["window_start"] == WIN_START.isoformat()
    assert row["window_end"] == WIN_END.isoformat()


def test_first_pass_waits_for_last_bucket(enabled: None) -> None:
    sb = FakeSB(devices=[dev(1), dev(2)],
                telemetry_30m=[{"device_id": "dev-0002", "bucket": LAST_BUCKET}])
    runner = AnalysisRunner(sb=sb)
    # 08:05 — 07:30 버킷이 집계된 dev-0002 만
    assert runner.tick(kst(8, 5)) == 1
    assert report(sb, "dev-0001") is None
    assert report(sb, "dev-0002")["pass"] == 1
    # 08:16 — 08:15 가 지났으니 버킷 없어도 생성
    assert runner.tick(kst(8, 16)) == 1
    row = report(sb, "dev-0001")
    assert row is not None and row["pass"] == 1
    assert row["temp_card"] is None and row["humid_card"] is None   # 카드 둘 다 None 이어도 행 저장
    assert row["generated_at"] == iso(kst(8, 16))


# ---------- 2단계 ----------

def test_second_pass_picks_backdated(enabled: None) -> None:
    sb = FakeSB(devices=[dev(1)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(8, 16)) == 1
    first = report(sb)
    assert first["pass"] == 1 and first["humid_card"] is None

    # Stage N D11: 08:00 전 시작 이벤트가 08시 이후 소급 INSERT 됨
    sb.rows("alerts").append({"id": "a-humid", **alert(
        "dev-0001", "env_humid_high", kst(7, 10), kst(7, 50), peak_value=92, threshold=80)})
    assert runner.tick(kst(8, 30)) == 0          # 1차는 이미 있음, 2차 전
    assert runner.tick(kst(9, 6)) == 1
    row = report(sb)
    assert row["pass"] == 2
    assert row["humid_card"]["title"] == "과습을 주의해 주세요"
    assert row["humid_card"]["lines"][0].startswith("11/3 07:10~07:50")
    assert len(sb.rows("env_daily_reports")) == 1


def test_two_pass_idempotent(enabled: None) -> None:
    sb = FakeSB(devices=[dev(1), dev(2)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(8, 16)) == 2
    assert runner.tick(kst(8, 21)) == 0          # 1차는 없을 때만
    sb.rows("alerts").append({"id": "a-late", **alert(
        "dev-0002", "env_humid_high", kst(7, 10), kst(7, 50), peak_value=92, threshold=80)})
    assert runner.tick(kst(9, 6)) == 1           # 2차: 바뀐 기기만 덮어씀
    assert runner.tick(kst(9, 11)) == 0
    assert runner.tick(kst(23, 0)) == 0
    rows = {r["device_id"]: r for r in sb.rows("env_daily_reports")}
    assert len(rows) == 2
    assert rows["dev-0001"]["pass"] == 1         # 내용이 같으면 다시 쓰지 않음(앱 Realtime 중복 방지)
    assert rows["dev-0002"]["pass"] == 2


def test_since_gates_reports(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # v4 를 켜기 전 구간을 "이벤트 없음"으로 분석하지 않는다
    monkeypatch.delenv("ALERTS_V4_SINCE")
    assert AnalysisRunner(sb=ExplodingSB()).tick(kst(9, 30)) == 0          # 값 없음 → 생성 안 함
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-11-02T15:00:00+09:00")    # 구간 도중 켬
    assert AnalysisRunner(sb=ExplodingSB()).tick(kst(9, 30)) == 0
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-11-02T08:00:00+09:00")    # 구간 시작과 같음
    sb = FakeSB(devices=[dev(1)])
    assert AnalysisRunner(sb=sb).tick(kst(9, 30)) == 1


def test_cursor_exact_batch_no_empty_tick(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis_runner, "BATCH_SIZE", 2)
    sb = FakeSB(devices=[dev(1), dev(2)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(8, 16)) == 2
    sb.rows("alerts").append({"id": "a-late", **alert(
        "dev-0001", "env_humid_high", kst(7, 10), kst(7, 50), peak_value=92, threshold=80)})
    assert runner.tick(kst(9, 6)) == 1           # 기기 수 = 배치 크기여도 빈 tick 없이 바로 2차


def test_done_day_skips_queries(enabled: None) -> None:
    sb = RecordingSB(devices=[dev(1)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(8, 16)) == 1
    assert runner.tick(kst(9, 6)) == 0           # 2차(내용 같음) → 그날 완료
    sb.touched.clear()
    assert runner.tick(kst(9, 11)) == 0
    assert runner.tick(kst(20, 0)) == 0
    assert sb.touched == []                      # 완료된 날은 조회도 안 함
    assert runner.tick(kst(8, 16, day=4)) == 1   # 다음 날은 다시


def test_restart_after_0905_writes_once(enabled: None) -> None:
    """08~09시에 브리지가 죽어 있었으면 살아난 첫 tick 에 바로 2차 내용으로 1회."""
    sb = FakeSB(devices=[dev(1)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(10, 0)) == 1
    assert report(sb)["pass"] == 2
    assert runner.tick(kst(10, 5)) == 0
    # 전날보다 오래된 날은 소급하지 않음
    assert sb.one("env_daily_reports", report_date="2026-11-01") is None


# ---------- 대상 기기·커서 (P10) ----------

def test_skips_unlinked_and_ownerless(enabled: None) -> None:
    sb = FakeSB(devices=[dev(1), dev(2, unlinked_at="2026-11-01T00:00:00+00:00"), dev(3, owner_id=None)])
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    assert [r["device_id"] for r in sb.rows("env_daily_reports")] == ["dev-0001"]


def test_cursor_rotation(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sb = FakeSB(devices=[dev(i) for i in range(1, 202)])
    real = analysis_runner.generate_daily

    def flaky(sb_: Any, device: dict[str, Any], *a: Any, **k: Any) -> bool:
        if device["id"] == "dev-0001":
            raise RuntimeError("항상 실패하는 기기")
        return real(sb_, device, *a, **k)

    monkeypatch.setattr(analysis_runner, "generate_daily", flaky)
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(8, 16)) == 199        # 1..200 중 1번 실패
    assert report(sb, "dev-0201") is None
    assert runner.tick(kst(8, 21)) == 1          # 커서가 201번으로 넘어감
    assert report(sb, "dev-0201") is not None
    assert len(sb.rows("env_daily_reports")) == 200


def test_device_error_isolated(enabled: None, monkeypatch: pytest.MonkeyPatch,
                               caplog: pytest.LogCaptureFixture) -> None:
    sb = FakeSB(devices=[dev(1), dev(2), dev(3)])
    real = analysis_runner._fetch_events

    def boom(sb_: Any, device_id: str, window: Any) -> list[dict[str, Any]]:
        if device_id == "dev-0002":
            raise RuntimeError("조회 실패")
        return real(sb_, device_id, window)

    monkeypatch.setattr(analysis_runner, "_fetch_events", boom)
    with caplog.at_level(logging.ERROR, logger="backend.analysis_runner"):
        assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 2
    assert {r["device_id"] for r in sb.rows("env_daily_reports")} == {"dev-0001", "dev-0003"}
    assert any("dev-0002" in r.getMessage() for r in caplog.records)


# ---------- 입력 조회 ----------

def test_inputs_overlap_rule_and_chain_root_outside_window(enabled: None) -> None:
    """P4 겹침 + context.rule=='v4' + 구간 밖 chain_root 행을 id 로 가져와 한 줄로 합침(P11)."""
    root = {"id": "a-root", **alert("dev-0001", "env_temp_high", kst(7, 0, day=2), kst(7, 50, day=2),
                                     level="danger", peak_value=29.6, threshold=28)}
    tail = {"id": "a-tail", **alert("dev-0001", "env_temp_high", kst(7, 50, day=2), kst(8, 20, day=2),
                                     peak_value=28.5, threshold=28, chain_root="a-root")}
    noise = [
        {"id": "a-legacy", **alert("dev-0001", "env_temp_low", kst(12), kst(13), rule=None)},     # v4 아님
        {"id": "a-before", **alert("dev-0001", "env_temp_low", kst(5, day=2), kst(6, day=2))},  # 구간 전 종료
        {"id": "a-after", **alert("dev-0001", "env_temp_low", kst(8, 0), None)},                # 구간 끝 이후 시작
        {"id": "a-other", **alert("dev-0002", "env_temp_low", kst(12, day=2), kst(13, day=2))},  # 다른 기기
        {"id": "a-off", **alert("dev-0001", "offline", kst(12, day=2), None)},                   # 다른 kind
    ]
    ongoing = {"id": "a-dry", **alert("dev-0001", "env_humid_low", kst(22, 0, day=2), None,
                                       peak_diff=9, threshold=60)}
    sb = FakeSB(devices=[dev(1)], alerts=[root, tail, ongoing, *noise])
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    row = report(sb)
    temp = row["temp_card"]
    assert temp["title"] == "위험한 온도가 기록되었어요"
    assert temp["lines"] == ["07:00~08:20 위험 온도 29.6℃ 감지"]
    assert {i for ev in temp["data"]["events"] for i in ev["ids"]} == {"a-root", "a-tail"}
    assert row["humid_card"]["title"] == "건조를 주의해 주세요"
    assert row["humid_card"]["lines"] == ["22:00~ 적정 습도보다 최대 -9% 이탈"]


def test_chain_middle_rows_outside_window_fetched(enabled: None) -> None:
    """구간 밖 chain 중간 행(무음 주의·D14 재경고)도 가져와 peak·ids 에 반영(P11)."""
    rows = [
        {"id": "c-root", **alert("dev-0001", "env_temp_high", kst(5, 0, day=2), kst(5, 30, day=2),
                                 level="danger", peak_value=29.5, threshold=26)},
        {"id": "c-s1", **alert("dev-0001", "env_temp_high", kst(5, 30, day=2), kst(6, 0, day=2),
                               peak_value=28.0, threshold=26, chain_root="c-root", deescalated_from="c-root")},
        {"id": "c-d14", **alert("dev-0001", "env_temp_high", kst(6, 0, day=2), kst(6, 40, day=2),
                                level="danger", peak_value=30.8, threshold=26, chain_root="c-root",
                                push_suppressed=True)},
        {"id": "c-s2", **alert("dev-0001", "env_temp_high", kst(6, 40, day=2), kst(8, 30, day=2),
                               peak_value=28.2, threshold=26, chain_root="c-root", deescalated_from="c-d14")},
    ]
    sb = FakeSB(devices=[dev(1)], alerts=rows)
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    temp = report(sb)["temp_card"]
    assert temp["lines"] == ["05:00~08:30 위험 온도 30.8℃ 감지"]
    assert temp["data"]["events"][0]["ids"] == ["c-root", "c-s1", "c-d14", "c-s2"]


def test_buckets_feed_daynight_card(enabled: None) -> None:
    buckets = []
    t = WIN_START
    while t < WIN_END:
        h = t.astimezone(KST).hour
        buckets.append({"device_id": "dev-0001", "bucket": t.isoformat(),
                        "t_a_avg": 25.1 if 8 <= h < 20 else 21.8, "t_a_count": 600,
                        "h_a_avg": 70.0, "h_a_count": 600})
        t += timedelta(minutes=30)
    # 구간 밖 버킷은 무시돼야 함
    buckets.append({"device_id": "dev-0001", "bucket": WIN_END.isoformat(), "t_a_avg": 40.0, "t_a_count": 600})
    sb = FakeSB(devices=[dev(1)], telemetry_30m=buckets)
    assert AnalysisRunner(sb=sb).tick(kst(8, 5)) == 1      # 07:30 버킷 있음 → 08:15 전에도 1차
    temp = report(sb)["temp_card"]
    assert temp["title"] == "주간과 야간 모두 적정 온도를 유지했어요"
    assert temp["data"]["diff_daynight"] == 3.3


def test_disconnect_card_uses_registration(enabled: None) -> None:
    # 앱 요청 2026-11-12: 수집이 모자란 날은 카드 자리에 ERR_DISCONNECT. 등록 시각은 devices.created_at
    sb = FakeSB(devices=[dev(1, created_at=iso(kst(12, day=1))),       # 구간 전 등록
                         dev(2, created_at=iso(kst(15, day=2)))])      # 구간 중 등록
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 2
    row = report(sb, "dev-0001")
    assert row["temp_card"]["code"] == "ERR_DISCONNECT" and row["humid_card"]["code"] == "ERR_DISCONNECT"
    assert row["temp_card"]["title"] == "데이터 수집이 원활하지 않았어요"
    other = report(sb, "dev-0002")
    assert other["temp_card"] is None and other["humid_card"] is None


def test_humid_cycles_flag_off_not_queried(enabled: None) -> None:
    sb = RecordingSB(devices=[dev(1)])
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    assert "humid_cycles" not in sb.touched
    assert report(sb)["humid_card"] is None   # P9: cycles 없음 → 습도 카드 없음


def test_humid_cycles_flag_on_queried(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUMID_CYCLES_ENABLED", "true")
    monkeypatch.setenv("HUMID_CYCLES_SINCE", "2026-11-01T00:00:00+09:00")
    cycles = [
        {"device_id": "dev-0001", "started_at": iso(kst(9, 0, day=2)), "dry_reached_at": iso(kst(11, 0, day=2))},
        {"device_id": "dev-0001", "started_at": iso(kst(4, 30, day=3)), "dry_reached_at": None},  # 05시 전 = 11/2 소속
        {"device_id": "dev-0001", "started_at": iso(kst(4, 30, day=2)), "dry_reached_at": None},  # 11/1 소속
        {"device_id": "dev-0001", "started_at": iso(kst(5, 0, day=3)), "dry_reached_at": None},   # 11/3 소속
        {"device_id": "dev-0002", "started_at": iso(kst(10, 0, day=2)), "dry_reached_at": None},  # 다른 기기
    ]
    sb = FakeSB(devices=[dev(1)], humid_cycles=cycles, telemetry_30m=full_buckets("dev-0001", WIN_START, 1, 25.0))
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    humid = report(sb)["humid_card"]
    assert humid["title"] == "건습 사이클이 건강하게 유지됐어요"
    assert humid["lines"] == ["습기 2번, 건기 1번이 기록됐어요"]


@pytest.mark.parametrize("since", ["2026-11-02T06:00:00+09:00", None])
def test_since_gate_daily(enabled: None, monkeypatch: pytest.MonkeyPatch, since: str | None) -> None:
    # 켠 시각이 사이클 구간(11/2 05:00~) 뒤거나 없으면 사이클을 안 본다 → "습기 미감지" 오카드 없음
    monkeypatch.setenv("HUMID_CYCLES_ENABLED", "true")
    if since:
        monkeypatch.setenv("HUMID_CYCLES_SINCE", since)
    else:
        monkeypatch.delenv("HUMID_CYCLES_SINCE", raising=False)
    sb = RecordingSB(devices=[dev(1)], telemetry_30m=full_buckets("dev-0001", WIN_START, 1, 25.0))
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    assert "humid_cycles" not in sb.touched
    assert report(sb)["humid_card"] is None


# ---------- 주간 (Stage Q Task 3) ----------
# 기준 주: week_start = 2026-11-02(월), 구간 11/2 08:00 ~ 11/9 08:00 KST. 11/9 은 월요일.

WEEK = "2026-11-02"
WEEK_START_UTC = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
WEEK_END_UTC = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)


@pytest.fixture
def weekly(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # 10/26 주까지 since 이후 — "지지난 주 행이 있어서 안 만든다"를 since 가 아니라 행으로 확인하려고
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-10-19T00:00:00+09:00")


def wreport(sb: FakeSB, week_start: str = WEEK, device_id: str = "dev-0001") -> dict[str, Any] | None:
    return sb.one("env_weekly_reports", device_id=device_id, week_start=week_start)


def full_buckets(device_id: str, start: datetime, days: int, t_avg: float) -> list[dict[str, Any]]:
    out = []
    t = start
    while t < start + timedelta(days=days):
        out.append({"device_id": device_id, "bucket": t.isoformat(),
                    "t_a_avg": t_avg, "t_a_count": 600, "h_a_avg": 70.0, "h_a_count": 600})
        t += timedelta(minutes=30)
    return out


def test_weekly_monday_after_8(weekly: None) -> None:
    prior = {"device_id": "dev-0001", "week_start": "2026-10-26", "temp_card": None,
             "humid_card": None, "notices": [], "generated_at": "2026-10-26T23:01:00+00:00"}
    sb = FakeSB(devices=[dev(1)], env_weekly_reports=[prior])
    runner = AnalysisRunner(sb=sb)
    runner.tick(kst(7, 59, day=9))               # 월 07:59 — 대상은 지지난 주(10/26), 이미 있음
    assert len(sb.rows("env_weekly_reports")) == 1
    assert wreport(sb) is None
    runner.tick(kst(8, 1, day=9))                # 월 08:01 — 소급 이벤트를 기다리느라 아직
    assert wreport(sb) is None
    runner.tick(kst(9, 6, day=9))                # 월 09:06 — 지난주(11/2) 생성 (일일 2차와 같은 시각)
    row = wreport(sb)
    assert row is not None
    assert row["window_start"] == WEEK_START_UTC.isoformat()
    assert row["window_end"] == WEEK_END_UTC.isoformat()
    assert row["generated_at"] == iso(kst(9, 6, day=9))
    assert row["temp_card"] is None and row["humid_card"] is None
    assert {n["code"] for n in row["notices"]} == {"ERR_NO_DATA"}   # 데이터 없음 → 지표별 안내


def test_weekly_recovery_tuesday(weekly: None) -> None:
    sb = FakeSB(devices=[dev(1), dev(2)])
    AnalysisRunner(sb=sb).tick(kst(10, 0, day=10))   # 월요일 tick 없음 → 화 10:00
    assert wreport(sb, device_id="dev-0001") is not None
    assert wreport(sb, device_id="dev-0002") is not None
    assert {r["week_start"] for r in sb.rows("env_weekly_reports")} == {WEEK}


def test_weekly_no_older_backfill(weekly: None) -> None:
    sb = FakeSB(devices=[dev(1)])
    AnalysisRunner(sb=sb).tick(kst(9, 6, day=16))    # 11/16(월) 09:05 이후 — 대상 11/9 주. 11/2 주는 없어도 소급 안 함
    assert wreport(sb, "2026-11-09") is not None
    assert wreport(sb) is None
    assert len(sb.rows("env_weekly_reports")) == 1


def test_weekly_idempotent(weekly: None) -> None:
    sb = FakeSB(devices=[dev(1)])
    runner = AnalysisRunner(sb=sb)
    runner.tick(kst(7, 0, day=10))
    first = dict(wreport(sb))
    runner.tick(kst(7, 5, day=10))
    AnalysisRunner(sb=sb).tick(kst(7, 10, day=10))   # 재시작(상태 없음)해도 행이 있으면 안 씀
    assert len(sb.rows("env_weekly_reports")) == 1
    assert wreport(sb)["generated_at"] == first["generated_at"]


def test_weekly_since_gate(weekly: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # v4 를 주 도중에 켰으면 그 주는 켜기 전 날들이 "이벤트 없음"으로 잡히므로 만들지 않는다
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-11-04T00:00:00+09:00")
    assert AnalysisRunner(sb=ExplodingSB()).tick(kst(7, 0, day=10)) == 0   # 일일은 08시 전, 주간은 since 전
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-11-02T08:00:00+09:00")    # 구간 시작과 같음
    sb = FakeSB(devices=[dev(1)])
    assert AnalysisRunner(sb=sb).tick(kst(7, 0, day=10)) == 1
    assert wreport(sb) is not None


def test_weekly_done_week_skips_queries(weekly: None) -> None:
    sb = RecordingSB(devices=[dev(1), dev(2)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(7, 0, day=10)) == 2     # 화 07:00 — 일일은 쉬고 주간만
    sb.touched.clear()
    assert runner.tick(kst(7, 5, day=10)) == 0
    assert runner.tick(kst(7, 59, day=16)) == 0    # 다음 주 월 09:05 전까지 같은 주 → 조회 없음
    assert sb.touched == []
    runner.tick(kst(9, 6, day=16))                 # 새 주(11/9)는 다시
    assert wreport(sb, "2026-11-09") is not None


def test_weekly_done_waits_for_full_cycle(weekly: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """배치가 여러 개면 한 바퀴를 다 돌아야 완료 — 앞 배치만 보고 쉬지 않는다."""
    monkeypatch.setattr(analysis_runner, "BATCH_SIZE", 2)
    sb = FakeSB(devices=[dev(i) for i in range(1, 4)])
    runner = AnalysisRunner(sb=sb)
    assert runner.tick(kst(7, 0, day=10)) == 2
    assert runner.tick(kst(7, 5, day=10)) == 1
    assert len(sb.rows("env_weekly_reports")) == 3


def test_weekly_and_daily_share_batch(weekly: None) -> None:
    sb = RecordingSB(devices=[dev(1), dev(2)])
    assert AnalysisRunner(sb=sb).tick(kst(9, 6, day=9)) == 4    # 일일 2 + 주간 2
    assert sb.touched.count("devices") == 1                     # 대상 조회는 한 번
    assert len(sb.rows("env_daily_reports")) == 2
    assert len(sb.rows("env_weekly_reports")) == 2


def test_weekly_device_error_isolated(weekly: None, monkeypatch: pytest.MonkeyPatch,
                                      caplog: pytest.LogCaptureFixture) -> None:
    sb = FakeSB(devices=[dev(1), dev(2), dev(3)])
    real = analysis_runner.generate_weekly

    def flaky(sb_: Any, device: dict[str, Any], *a: Any, **k: Any) -> bool:
        if device["id"] == "dev-0002":
            raise RuntimeError("주간 실패")
        return real(sb_, device, *a, **k)

    monkeypatch.setattr(analysis_runner, "generate_weekly", flaky)
    runner = AnalysisRunner(sb=sb)
    with caplog.at_level(logging.ERROR, logger="backend.analysis_runner"):
        assert runner.tick(kst(7, 0, day=10)) == 2
    assert {r["device_id"] for r in sb.rows("env_weekly_reports")} == {"dev-0001", "dev-0003"}
    assert any("dev-0002" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(analysis_runner, "generate_weekly", real)
    assert runner.tick(kst(7, 5, day=10)) == 1     # 실패 기기는 완료로 치지 않고 다음 바퀴에 재시도


def test_weekly_includes_monday_backdated_event(weekly: None) -> None:
    # 월 07:10 시작 건조 주의가 08:10 에 소급 기록돼도 주간(09:05 이후 생성)에 들어간다
    buckets = full_buckets("dev-0001", WEEK_START_UTC, 7, 24.0)
    early = [{"id": f"h-{d}", **alert("dev-0001", "env_humid_low", kst(10, 0, day=d), kst(11, 0, day=d),
                                       peak_diff=8, threshold=40)} for d in (3, 5)]
    sb = FakeSB(devices=[dev(1, created_at="2026-10-01T00:00:00+00:00")], telemetry_30m=buckets,
                alerts=early)
    runner = AnalysisRunner(sb=sb)
    runner.tick(kst(8, 5, day=9))
    sb.rows("alerts").append({"id": "h-mon", **alert("dev-0001", "env_humid_low", kst(7, 10, day=9),
                                                      kst(7, 50, day=9), peak_diff=9, threshold=40)})
    runner.tick(kst(9, 6, day=9))
    humid = wreport(sb)["humid_card"]
    assert humid["code"] == "HUM_DRY"
    assert humid["lines"] == ["총 3회 건조 주의가 발생. 사육장에 수분을 보충해 주세요"]


def test_failing_device_given_up_after_3(weekly: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # 계속 실패하는 기기 하나 때문에 일주일·하루 내내 재시도하지 않는다
    sb = RecordingSB(devices=[dev(1), dev(2)])
    real_w, real_d = analysis_runner.generate_weekly, analysis_runner.generate_daily

    def bad_w(sb_: Any, device: dict[str, Any], *a: Any, **k: Any) -> bool:
        if device["id"] == "dev-0002":
            raise RuntimeError("주간 계속 실패")
        return real_w(sb_, device, *a, **k)

    def bad_d(sb_: Any, device: dict[str, Any], *a: Any, **k: Any) -> bool:
        if device["id"] == "dev-0002":
            raise RuntimeError("일일 계속 실패")
        return real_d(sb_, device, *a, **k)

    monkeypatch.setattr(analysis_runner, "generate_weekly", bad_w)
    monkeypatch.setattr(analysis_runner, "generate_daily", bad_d)
    runner = AnalysisRunner(sb=sb)
    for m in (6, 11, 16):                          # 3번째 실패에서 포기 → 그 바퀴로 완료
        runner.tick(kst(9, m, day=10))
    sb.touched.clear()
    assert runner.tick(kst(9, 21, day=10)) == 0
    assert sb.touched == []                        # 일일·주간 모두 완료 — 조회 없음
    assert wreport(sb, device_id="dev-0002") is None


def test_weekly_prev_week_buckets_temp_diff_high(weekly: None) -> None:
    prev_start = WEEK_START_UTC - timedelta(days=7)
    buckets = [*full_buckets("dev-0001", prev_start, 7, 21.8),
               *full_buckets("dev-0001", WEEK_START_UTC, 7, 25.0)]
    sb = FakeSB(devices=[dev(1, created_at="2026-10-01T00:00:00+00:00")], telemetry_30m=buckets)
    AnalysisRunner(sb=sb).tick(kst(7, 0, day=10))
    row = wreport(sb)
    temp = row["temp_card"]
    assert temp["code"] == "TEMP_DIFF_HIGH"
    assert temp["title"] == "지난주보다 평균 온도가 높아졌어요"
    assert temp["lines"] == ["지난주 대비 평균 온도가 3.2℃ 상승했어요"]
    assert row["notices"] == []


def test_weekly_inputs_events_and_registration(weekly: None) -> None:
    """이벤트(겹침·chain)와 devices.created_at(누락률) 이 주간 계산에 들어간다."""
    buckets = full_buckets("dev-0001", WEEK_START_UTC + timedelta(days=3), 4, 24.0)   # 목 08:00 이후만
    events = [
        {"id": "w-1", **alert("dev-0001", "env_temp_high", kst(14, 0, day=5), kst(15, 0, day=5),
                              level="danger", peak_value=30.1, threshold=26, user_max=26)},
        {"id": "w-old", **alert("dev-0001", "env_temp_high", kst(14, 0, day=1), kst(15, 0, day=1),
                                level="danger", peak_value=33.0, threshold=26, user_max=26)},   # 지난주
    ]
    reg = iso(kst(8, 0, day=5))   # 목 08:00 등록 → 그 뒤 표본이 꽉 차 있으니 누락률 0
    sb = FakeSB(devices=[dev(1, created_at=reg)], telemetry_30m=buckets, alerts=events)
    AnalysisRunner(sb=sb).tick(kst(7, 0, day=10))
    row = wreport(sb)
    assert row["temp_card"]["code"] == "TEMP_DANGER"
    assert row["temp_card"]["data"]["n_danger"] == 1
    assert row["temp_card"]["data"]["missing_rate"] == 0.0
    assert row["notices"] == []


# ---------- 스레드 ----------

def test_runner_start_stop_cheap_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV_REPORTS_ENABLED", raising=False)
    runner = AnalysisRunner(interval_sec=0.01, sb=ExplodingSB())
    runner.start()
    runner.start()   # 두 번 불러도 스레드 하나
    runner.stop()


@pytest.mark.parametrize("since, code", [
    ("2026-11-02T04:00:00+09:00", "HUM_NO_MIST"),   # 주 사이클 구간(월 05:00~) 전부터 켜짐 → ㉕ 카드
    ("2026-11-03T00:00:00+09:00", None),            # 주 중간에 켬 → 사이클을 안 봄(부분 주 오카드 방지)
])
def test_since_gate_weekly(weekly: None, monkeypatch: pytest.MonkeyPatch, since: str, code: str | None) -> None:
    monkeypatch.setenv("HUMID_CYCLES_ENABLED", "true")
    monkeypatch.setenv("HUMID_CYCLES_SINCE", since)
    buckets = full_buckets("dev-0001", WEEK_START_UTC, 7, 24.0)
    sb = FakeSB(devices=[dev(1, created_at="2026-10-01T00:00:00+00:00")], telemetry_30m=buckets, humid_cycles=[])
    AnalysisRunner(sb=sb).tick(kst(9, 6, day=9))
    humid = wreport(sb)["humid_card"]
    assert (humid or {}).get("code") == code
