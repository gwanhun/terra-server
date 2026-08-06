"""GME A/B 오케스트레이터 — 모드에 따라 목적지 계산~복사까지. 전부 주입 구조.

흐름 (mode 별로 게이팅):
  off        → 아무 작업 안 함 (분석조차 안 함)
  ↓ (off 아니면)
  금지 모드(production_copy) 거부 → 분석(주입) → 핀 가드(fail-closed)
  → 계약 검증(analysis_status·3상태, fail-closed 격리) → 목적지 라우팅
  → dry_run: 계산만 / test_copy: 승인된 test prefix 로 원본보존 복사+크기·해시검증+멱등
  → DB 감사 기록(주입)

불변식: 원본 이동·삭제 없음, production 경로 write 없음. torch 무관(analyzer 주입).
로그(safe_log_line)에는 경로·dst_key·clip id 를 넣지 않는다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .mode import GmeMode, assert_mode_allowed
from .pin import CANONICAL_PIN, ExpectedPin, PinMismatch, observed_from_result, verify_pin
from .routing import DEST_RETRY_OR_QUARANTINE, route_decision

# 승인된 test 전용 목적지 prefix. 운영 canonical(terra-clips/clips/)·capture(test/)와 분리.
TEST_COPY_DEST_PREFIX = "gme-test/"

_VALID_DECISIONS = frozenset({"detected_candidate", "not_observed", "unresolved"})


# ---------- 주입 인터페이스 ----------


class PresenceLike(Protocol):
    decision: str
    reason_code: str
    analysis_status: str
    schema_version: str
    model_name: str
    algorithm_version: str
    engine_schema_version: str
    checkpoint_sha256: str
    threshold: float


class R2Copier(Protocol):
    def exists(self, key: str) -> bool: ...
    def size(self, key: str) -> int: ...
    def sha256(self, key: str) -> str: ...
    def copy(self, src_key: str, dst_key: str) -> None: ...


class AuditRecorder(Protocol):
    def record(self, record: Mapping[str, Any]) -> None: ...


Analyzer = Callable[[str], PresenceLike]
Clock = Callable[[], float]


class CopyVerifyError(RuntimeError):
    """복사 후 크기/해시 불일치 또는 목적지 충돌 (fail-closed)."""


@dataclass(frozen=True, slots=True)
class GmeOutcome:
    mode: str
    decision: str | None
    destination: str | None
    action: str            # none|planned|copied|skipped_idempotent|copy_failed|quarantined
    reason: str            # 계약/핀/예외 사유 (ok 면 "")
    error_code: str
    elapsed_ms: int
    dst_key: str | None = None
    copied: bool = False
    verify_ok: bool | None = None
    idempotent: bool = False
    audit_recorded: bool = False
    # 안전 불변식 — 어떤 모드에서도 원본 이동/삭제·운영 write 없음
    source_preserved: bool = True
    source_moved: bool = False
    source_deleted: bool = False
    production_write: bool = False

    @property
    def r2_write(self) -> bool:
        return self.copied

    @property
    def db_write(self) -> bool:
        return self.audit_recorded

    def safe_log_line(self) -> str:
        """경로·dst_key·clip id 없이 로깅 가능한 한 줄."""
        return (
            f"gme mode={self.mode} decision={self.decision} destination={self.destination} "
            f"action={self.action} reason={self.reason or '-'} r2_write={self.r2_write} "
            f"db_write={self.db_write} elapsed_ms={self.elapsed_ms} error={self.error_code or '-'}"
        )


# ---------- 내부 헬퍼 ----------


def _basename(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _resolve_destination_fail_closed(result: PresenceLike) -> tuple[str, str]:
    """서버 독립 계약 검증. analysis_status!=ok 이거나 판정이 비정상이면 격리(fail-closed).

    반환: (destination, reason). reason=='ok' 면 정상 라우팅.
    """
    status = getattr(result, "analysis_status", None)
    decision = getattr(result, "decision", None)
    if decision not in _VALID_DECISIONS:
        return DEST_RETRY_OR_QUARANTINE, "invalid_decision"
    if status != "ok":
        # 분석 실패는 무조건 격리. A/B 판정을 주장하면 계약 위반.
        return DEST_RETRY_OR_QUARANTINE, ("status_decision_mismatch" if decision != "unresolved" else "analysis_not_ok")
    return route_decision(decision), "ok"


def _do_test_copy(r2: R2Copier | None, source_key: str, dst_key: str) -> tuple[str, bool]:
    """원본 보존 복사 + 크기·해시 검증 + 멱등. 반환: (action, verify_ok)."""
    if r2 is None:
        raise CopyVerifyError("test_copy 인데 R2Copier 미주입")
    src_size = r2.size(source_key)
    src_hash = r2.sha256(source_key)
    if r2.exists(dst_key):
        # 멱등: 같은 내용이면 skip, 다른 내용이면 덮어쓰지 않고 거부(fail-closed)
        if r2.sha256(dst_key) == src_hash:
            return "skipped_idempotent", True
        raise CopyVerifyError("목적지에 다른 내용 존재 — 덮어쓰지 않음")
    r2.copy(source_key, dst_key)  # 원본은 그대로 (copy, not move)
    if r2.size(dst_key) != src_size or r2.sha256(dst_key) != src_hash:
        raise CopyVerifyError("복사본 크기/해시 불일치")
    return "copied", True


def _audit(auditor: AuditRecorder | None, outcome: GmeOutcome, source_key: str) -> bool:
    if auditor is None:
        return False
    auditor.record({
        "mode": outcome.mode,
        "decision": outcome.decision,
        "destination": outcome.destination,
        "action": outcome.action,
        "reason": outcome.reason,
        "error_code": outcome.error_code,
        "source_key": source_key,   # DB 감사 추적용 (로그엔 안 나감)
        "dst_key": outcome.dst_key,
        "elapsed_ms": outcome.elapsed_ms,
    })
    return True


# ---------- 공개 진입점 ----------


def process_clip(
    source_key: str,
    *,
    mode: GmeMode,
    analyzer: Analyzer,
    r2: R2Copier | None = None,
    auditor: AuditRecorder | None = None,
    runtime_commit: str | None = None,
    expected_pin: ExpectedPin = CANONICAL_PIN,
    test_copy_prefix: str = TEST_COPY_DEST_PREFIX,
    clock: Clock = time.monotonic,
) -> GmeOutcome:
    """clip 하나를 모드에 맞춰 처리한다. 부수효과는 모드가 허용한 것만."""
    start = clock()

    # 1. off — 아무 작업도 안 함 (분석조차 안 함). 운영 기본값.
    if mode == GmeMode.OFF:
        return GmeOutcome(
            mode=mode.value, decision=None, destination=None, action="none",
            reason="", error_code="", elapsed_ms=int((clock() - start) * 1000),
        )

    # 2. 금지 모드 거부 (production_copy)
    assert_mode_allowed(mode)

    # 3. 분석 (주입) — 예외는 fail-closed 격리
    try:
        result = analyzer(source_key)
    except Exception as exc:  # noqa: BLE001 - 격리 경계
        out = GmeOutcome(
            mode=mode.value, decision=None, destination=DEST_RETRY_OR_QUARANTINE,
            action="quarantined", reason="analyzer_exception",
            error_code=f"exception:{type(exc).__name__}", elapsed_ms=int((clock() - start) * 1000),
        )
        object.__setattr__(out, "audit_recorded", _audit(auditor, out, source_key))
        return out

    # 4. 런타임 핀 가드 (fail-closed)
    try:
        verify_pin(observed_from_result(result, runtime_commit=runtime_commit), expected_pin)
    except PinMismatch as exc:
        out = GmeOutcome(
            mode=mode.value, decision=getattr(result, "decision", None),
            destination=DEST_RETRY_OR_QUARANTINE, action="quarantined", reason="pin_mismatch",
            error_code=str(exc), elapsed_ms=int((clock() - start) * 1000),
        )
        object.__setattr__(out, "audit_recorded", _audit(auditor, out, source_key))
        return out

    # 5. 서버 독립 계약 검증 → 목적지 (fail-closed 격리 포함)
    destination, reason = _resolve_destination_fail_closed(result)
    decision = getattr(result, "decision", None)

    # 6. 모드별 액션
    dst_key: str | None = None
    action = "planned"
    verify_ok: bool | None = None
    copied = False
    idempotent = False
    error_code = ""

    if mode == GmeMode.TEST_COPY:
        dst_key = f"{test_copy_prefix}{destination}/{_basename(source_key)}"
        try:
            action, verify_ok = _do_test_copy(r2, source_key, dst_key)
            copied = action == "copied"
            idempotent = action == "skipped_idempotent"
        except Exception as exc:  # noqa: BLE001 - copy 실패는 fail-closed, 원본 그대로
            action = "copy_failed"
            verify_ok = False
            error_code = f"copy_error:{type(exc).__name__}"
    # dry_run: action 은 'planned' 유지, write 없음

    out = GmeOutcome(
        mode=mode.value, decision=decision, destination=destination, action=action,
        reason=reason, error_code=error_code, elapsed_ms=int((clock() - start) * 1000),
        dst_key=dst_key, copied=copied, verify_ok=verify_ok, idempotent=idempotent,
    )
    object.__setattr__(out, "audit_recorded", _audit(auditor, out, source_key))
    return out
