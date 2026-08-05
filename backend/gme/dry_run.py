"""GME presence dry-run: 판정 → 목적지 **계산만**. 이동·복사·DB/R2 write 없음.

1차 구현 안전장치:
- 원본 입력 보존 (source_preserved=True), 자동 삭제 구현 안 함.
- 실제 R2 이동/복사 없음 (performed_move=False), DB write 없음, R2 write 없음.
- 분석 실패는 fail-closed → RETRY_OR_QUARANTINE (절대 B 아님).
- 로그에는 decision·목적지·일반화된 오류·버전·처리시간만. 영상 경로·R2 key·
  credential 은 어떤 필드에도 담지 않는다.

analyzer 는 주입받는다 → terra-server 런타임에 torch 를 끌어오지 않고,
테스트는 fake analyzer 로 순수하게 검증 가능. 실제 analyzer 는 핀된 commit
(PINNED_GME_COMMIT) 의 gecko-vision-gate `gme_presence` 를 쓴다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .routing import DEST_RETRY_OR_QUARANTINE, route_decision


class PresenceLike(Protocol):
    """gecko-vision-gate `PresenceResult` 의 최소 형태 (duck typing)."""

    decision: str
    reason_code: str
    analysis_status: str
    schema_version: str
    model_name: str
    algorithm_version: str
    checkpoint_sha256: str


Analyzer = Callable[[str], PresenceLike]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    # 판정/목적지
    decision: str | None
    destination: str
    reason_code: str
    analysis_status: str | None
    # 버전 identity (경로·credential 아님 → 로그 가능)
    schema_version: str | None
    model_name: str | None
    algorithm_version: str | None
    checkpoint_sha256: str | None
    # 운영
    elapsed_ms: int
    error_code: str
    # 안전장치 플래그 — dry-run 은 어떤 부수효과도 내지 않음
    source_preserved: bool = True
    performed_move: bool = False
    db_write: bool = False
    r2_write: bool = False

    def safe_log_line(self) -> str:
        """영상 경로·R2 key·credential 없이 로깅 가능한 한 줄."""
        ck = (self.checkpoint_sha256 or "")[:16]
        return (
            f"gme_dry_run decision={self.decision} destination={self.destination} "
            f"status={self.analysis_status} schema={self.schema_version} "
            f"model={self.model_name} algo={self.algorithm_version} "
            f"ckpt={ck} elapsed_ms={self.elapsed_ms} error={self.error_code or '-'}"
        )


def _plan_from_result(result: PresenceLike, elapsed_ms: int) -> DryRunPlan:
    decision = getattr(result, "decision", None)
    return DryRunPlan(
        decision=decision,
        destination=route_decision(decision),
        reason_code=getattr(result, "reason_code", ""),
        analysis_status=getattr(result, "analysis_status", None),
        schema_version=getattr(result, "schema_version", None),
        model_name=getattr(result, "model_name", None),
        algorithm_version=getattr(result, "algorithm_version", None),
        checkpoint_sha256=getattr(result, "checkpoint_sha256", None),
        elapsed_ms=elapsed_ms,
        error_code="",
    )


def _plan_failed(error_code: str, elapsed_ms: int) -> DryRunPlan:
    # fail-closed: 분석 실패 → 격리. B/A 아님.
    return DryRunPlan(
        decision=None,
        destination=DEST_RETRY_OR_QUARANTINE,
        reason_code="",
        analysis_status=None,
        schema_version=None,
        model_name=None,
        algorithm_version=None,
        checkpoint_sha256=None,
        elapsed_ms=elapsed_ms,
        error_code=error_code,
    )


def analyze_and_plan(
    clip_path: str,
    *,
    analyzer: Analyzer,
    clock: Clock = time.monotonic,
) -> DryRunPlan:
    """clip 을 analyzer 로 분석하고 목적지를 계산한다. 부수효과 없음.

    analyzer 예외는 잡아서 일반화된 오류 코드로만 남기고 격리 목적지를 반환한다.
    """
    start = clock()
    try:
        result = analyzer(clip_path)
    except Exception as exc:  # noqa: BLE001 - 격리 경계, 일반화된 코드만 노출
        return _plan_failed(f"exception:{type(exc).__name__}", int((clock() - start) * 1000))
    return _plan_from_result(result, int((clock() - start) * 1000))
