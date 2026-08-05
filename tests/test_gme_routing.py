"""GME presence 라우팅 + dry-run 단위 테스트.

핵심 안전 성질:
- 3상태 매핑이 계약대로다.
- unresolved 는 절대 B 로 가지 않는다 (fail-closed).
- dry-run 은 어떤 부수효과(이동·DB/R2 write)도 내지 않는다.
- 분석 실패는 격리로 떨어지고 일반화된 오류 코드만 남긴다.
- 로그 한 줄에 경로·credential 이 새지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.gme.dry_run import DryRunPlan, analyze_and_plan
from backend.gme.routing import (
    DEST_A,
    DEST_B_REVIEW,
    DEST_RETRY_OR_QUARANTINE,
    route_decision,
)


# ---- routing ----

def test_route_detected_candidate_to_a() -> None:
    assert route_decision("detected_candidate") == DEST_A


def test_route_not_observed_to_b_review() -> None:
    assert route_decision("not_observed") == DEST_B_REVIEW


def test_route_unresolved_to_retry_quarantine() -> None:
    assert route_decision("unresolved") == DEST_RETRY_OR_QUARANTINE


@pytest.mark.parametrize("decision", ["unresolved", "", None, "garbage", "DETECTED_CANDIDATE"])
def test_only_not_observed_ever_routes_to_b(decision: str | None) -> None:
    """not_observed 외의 어떤 판정도 B_REVIEW 로 가면 안 된다."""
    if decision == "not_observed":
        return
    assert route_decision(decision) != DEST_B_REVIEW


def test_unknown_decision_is_fail_closed() -> None:
    assert route_decision("wat") == DEST_RETRY_OR_QUARANTINE
    assert route_decision(None) == DEST_RETRY_OR_QUARANTINE


# ---- dry-run ----

@dataclass
class _FakePresence:
    decision: str
    reason_code: str = "test"
    analysis_status: str = "ok"
    schema_version: str = "gme-presence-v1"
    model_name: str = "rf-detr-nano"
    algorithm_version: str = "gme-motion-v0"
    checkpoint_sha256: str = "cd1162b4c95041bc9b1ec064bb82ff67cb7d7416b2c778230ea0a59e2f2bef17"


def _fixed_clock():
    ticks = iter([1.0, 1.25])
    return lambda: next(ticks)


@pytest.mark.parametrize(
    "decision,expected",
    [
        ("detected_candidate", DEST_A),
        ("not_observed", DEST_B_REVIEW),
        ("unresolved", DEST_RETRY_OR_QUARANTINE),
    ],
)
def test_dry_run_maps_decision_to_destination(decision: str, expected: str) -> None:
    plan = analyze_and_plan(
        "irrelevant-clip",
        analyzer=lambda _p: _FakePresence(decision=decision),
        clock=_fixed_clock(),
    )
    assert plan.decision == decision
    assert plan.destination == expected


def test_dry_run_has_no_side_effects() -> None:
    plan = analyze_and_plan(
        "clip", analyzer=lambda _p: _FakePresence(decision="detected_candidate")
    )
    assert plan.source_preserved is True
    assert plan.performed_move is False
    assert plan.db_write is False
    assert plan.r2_write is False


def test_dry_run_records_elapsed_and_version() -> None:
    plan = analyze_and_plan(
        "clip",
        analyzer=lambda _p: _FakePresence(decision="detected_candidate"),
        clock=_fixed_clock(),
    )
    assert plan.elapsed_ms == 250  # (1.25 - 1.0) * 1000
    assert plan.schema_version == "gme-presence-v1"
    assert plan.checkpoint_sha256.startswith("cd1162b4")


def test_dry_run_analyzer_exception_is_fail_closed() -> None:
    def _boom(_p: str):
        raise ValueError("corrupt")

    plan = analyze_and_plan("clip", analyzer=_boom)
    assert plan.destination == DEST_RETRY_OR_QUARANTINE
    assert plan.destination != DEST_B_REVIEW
    assert plan.decision is None
    assert plan.error_code == "exception:ValueError"
    # 실패해도 부수효과 없음
    assert plan.db_write is False and plan.r2_write is False and plan.performed_move is False


def test_analysis_status_failure_does_not_reach_b() -> None:
    """analysis_status 가 실패여도 wrapper 가 unresolved 를 주면 격리로 간다."""
    plan = analyze_and_plan(
        "clip",
        analyzer=lambda _p: _FakePresence(decision="unresolved", analysis_status="decode_error"),
    )
    assert plan.destination == DEST_RETRY_OR_QUARANTINE
    assert plan.destination != DEST_B_REVIEW


def test_safe_log_line_leaks_no_path_or_credential() -> None:
    secret_path = "/Users/gwanhun/gme-real-smoke/gme-real-smoke-01.mp4"
    plan = analyze_and_plan(
        secret_path,
        analyzer=lambda _p: _FakePresence(decision="detected_candidate"),
    )
    line = plan.safe_log_line()
    assert secret_path not in line
    assert "gme-real-smoke" not in line
    assert "r2.cloudflarestorage.com" not in line
    # 로그에 있어야 할 것: decision·목적지·버전·시간
    assert "decision=detected_candidate" in line
    assert "destination=A" in line
