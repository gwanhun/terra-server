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
    monkeypatch.setenv("ALERTS_V4_SINCE", "2026-10-01T00:00:00+09:00")
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

    # Stage N D11: 07:10 시작 과습이 08:10 에 소급 INSERT 됨
    sb.rows("alerts").append({"id": "a-humid", **alert(
        "dev-0001", "env_humid_high", kst(7, 10), kst(7, 50), peak_value=92, threshold=80)})
    assert runner.tick(kst(8, 30)) == 0          # 1차는 이미 있음, 2차 전
    assert runner.tick(kst(9, 6)) == 1
    row = report(sb)
    assert row["pass"] == 2
    assert row["humid_card"]["title"] == "과습을 주의해 주세요"
    assert row["humid_card"]["lines"][0].startswith("07:10~07:50")
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
    assert temp["lines"] == ["11/2 07:00~08:20 위험 온도 29.6℃ 감지"]
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
    assert temp["lines"] == ["11/2 05:00~08:30 위험 온도 30.8℃ 감지"]
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


def test_humid_cycles_flag_off_not_queried(enabled: None) -> None:
    sb = RecordingSB(devices=[dev(1)])
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    assert "humid_cycles" not in sb.touched
    assert report(sb)["humid_card"] is None   # P9: cycles 없음 → 습도 카드 없음


def test_humid_cycles_flag_on_queried(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUMID_CYCLES_ENABLED", "true")
    cycles = [
        {"device_id": "dev-0001", "started_at": iso(kst(9, 0, day=2)), "dry_reached_at": iso(kst(11, 0, day=2))},
        {"device_id": "dev-0001", "started_at": iso(kst(21, 0, day=2)), "dry_reached_at": None},
        {"device_id": "dev-0001", "started_at": iso(kst(9, 0, day=1)), "dry_reached_at": None},   # 구간 밖
        {"device_id": "dev-0002", "started_at": iso(kst(10, 0, day=2)), "dry_reached_at": None},  # 다른 기기
    ]
    sb = FakeSB(devices=[dev(1)], humid_cycles=cycles)
    assert AnalysisRunner(sb=sb).tick(kst(8, 16)) == 1
    humid = report(sb)["humid_card"]
    assert humid["title"] == "건습 사이클이 건강하게 유지됐어요"
    assert humid["lines"] == ["습기 2번 , 건기 1번이 기록됐어요"]


# ---------- 스레드 ----------

def test_runner_start_stop_cheap_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV_REPORTS_ENABLED", raising=False)
    runner = AnalysisRunner(interval_sec=0.01, sb=ExplodingSB())
    runner.start()
    runner.start()   # 두 번 불러도 스레드 하나
    runner.stop()
