"""GME 파이프라인 오케스트레이터 테스트 — 모드 게이팅·fail-closed·copy·멱등·감사."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from backend.gme import PINNED_GME_COMMIT
from backend.gme.mode import GmeMode, GmeModeForbidden
from backend.gme.pin import CANONICAL_PIN
from backend.gme.pipeline import TEST_COPY_DEST_PREFIX, process_clip

SRC = "test/p4cam-79b5d844/2026-08-06/120000_abcdef12-3456-7890-abcd-ef1234567890.mp4"


@dataclass
class FakePresence:
    decision: str
    analysis_status: str = "ok"
    reason_code: str = "test"
    schema_version: str = CANONICAL_PIN.schema_version
    model_name: str = CANONICAL_PIN.model_name
    algorithm_version: str = CANONICAL_PIN.algorithm_version
    engine_schema_version: str = CANONICAL_PIN.engine_schema_version
    checkpoint_sha256: str = CANONICAL_PIN.checkpoint_sha256
    threshold: float = CANONICAL_PIN.threshold


def analyzer_for(decision: str, **over):
    def _a(_key: str) -> FakePresence:
        return FakePresence(decision=decision, **over)
    return _a


class FakeR2:
    def __init__(self, objects: dict[str, str] | None = None):
        self.objects = dict(objects or {SRC: "video-bytes"})
        self.copies: list[tuple[str, str]] = []
        self.corrupt_on_copy = False

    def exists(self, key): return key in self.objects
    def size(self, key): return len(self.objects[key].encode())
    def sha256(self, key): return hashlib.sha256(self.objects[key].encode()).hexdigest()

    def copy(self, src, dst):
        self.objects[dst] = "CORRUPT" if self.corrupt_on_copy else self.objects[src]
        self.copies.append((src, dst))


class FakeAuditor:
    def __init__(self): self.records = []
    def record(self, r): self.records.append(dict(r))


def _commit_kwargs():
    return {"runtime_commit": PINNED_GME_COMMIT}


# ---------- off ----------

def test_off_does_nothing_and_skips_analysis() -> None:
    called = []
    def spy(_k):
        called.append(1); return FakePresence("detected_candidate")
    out = process_clip(SRC, mode=GmeMode.OFF, analyzer=spy)
    assert out.action == "none"
    assert out.destination is None
    assert called == []                       # 분석조차 안 함
    assert not out.r2_write and not out.db_write and out.source_preserved


# ---------- production_copy 금지 ----------

def test_production_copy_forbidden() -> None:
    with pytest.raises(GmeModeForbidden):
        process_clip(SRC, mode=GmeMode.PRODUCTION_COPY,
                     analyzer=analyzer_for("detected_candidate"), **_commit_kwargs())


# ---------- dry_run ----------

@pytest.mark.parametrize(
    "decision,dest",
    [
        ("detected_candidate", "A"),
        ("not_observed", "B_REVIEW"),
        ("unresolved", "RETRY_OR_QUARANTINE"),
    ],
)
def test_dry_run_computes_destination_no_write(decision, dest) -> None:
    out = process_clip(SRC, mode=GmeMode.DRY_RUN, analyzer=analyzer_for(decision), **_commit_kwargs())
    assert out.destination == dest
    assert out.action == "planned"
    assert not out.r2_write and not out.production_write
    assert out.source_preserved and not out.source_moved and not out.source_deleted


# ---------- fail-closed: 계약 ----------

def test_status_not_ok_is_quarantined_even_if_decision_ab() -> None:
    # 판정은 detected_candidate 인데 analysis_status 가 실패 → 격리 (A 아님)
    out = process_clip(SRC, mode=GmeMode.DRY_RUN,
                       analyzer=analyzer_for("detected_candidate", analysis_status="decode_error"),
                       **_commit_kwargs())
    assert out.destination == "RETRY_OR_QUARANTINE"
    assert out.reason == "status_decision_mismatch"


def test_invalid_decision_is_quarantined() -> None:
    out = process_clip(SRC, mode=GmeMode.DRY_RUN, analyzer=analyzer_for("garbage"), **_commit_kwargs())
    assert out.destination == "RETRY_OR_QUARANTINE"
    assert out.reason == "invalid_decision"


# ---------- fail-closed: 핀 ----------

def test_pin_mismatch_quarantines() -> None:
    out = process_clip(SRC, mode=GmeMode.TEST_COPY,
                       analyzer=analyzer_for("detected_candidate", checkpoint_sha256="0" * 64),
                       r2=FakeR2(), **_commit_kwargs())
    assert out.action == "quarantined"
    assert out.reason == "pin_mismatch"
    assert not out.r2_write                     # 핀 불일치면 복사 안 함


def test_wrong_runtime_commit_quarantines() -> None:
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=analyzer_for("detected_candidate"),
                       r2=FakeR2(), runtime_commit="wrongcommit")
    assert out.reason == "pin_mismatch"


# ---------- test_copy ----------

def test_test_copy_copies_and_verifies() -> None:
    r2 = FakeR2()
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=analyzer_for("detected_candidate"),
                       r2=r2, **_commit_kwargs())
    assert out.destination == "A"
    assert out.action == "copied"
    assert out.copied and out.verify_ok is True and out.r2_write
    assert out.dst_key == f"{TEST_COPY_DEST_PREFIX}A/{SRC.rsplit('/', 1)[-1]}"
    assert out.dst_key in r2.objects            # 복사됨
    assert SRC in r2.objects                    # 원본 그대로
    assert out.source_preserved and not out.source_moved and not out.source_deleted


def test_test_copy_idempotent_skip() -> None:
    r2 = FakeR2()
    dst = f"{TEST_COPY_DEST_PREFIX}A/{SRC.rsplit('/', 1)[-1]}"
    r2.objects[dst] = r2.objects[SRC]           # 이미 같은 내용 존재
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=analyzer_for("detected_candidate"),
                       r2=r2, **_commit_kwargs())
    assert out.action == "skipped_idempotent"
    assert out.idempotent and not out.copied
    assert r2.copies == []                       # 실제 copy 호출 안 함


def test_test_copy_conflict_does_not_overwrite() -> None:
    r2 = FakeR2()
    dst = f"{TEST_COPY_DEST_PREFIX}A/{SRC.rsplit('/', 1)[-1]}"
    r2.objects[dst] = "DIFFERENT"                # 다른 내용이 이미 존재
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=analyzer_for("detected_candidate"),
                       r2=r2, **_commit_kwargs())
    assert out.action == "copy_failed"
    assert out.verify_ok is False
    assert r2.objects[dst] == "DIFFERENT"        # 안 덮어씀


def test_test_copy_verify_fail() -> None:
    r2 = FakeR2(); r2.corrupt_on_copy = True
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=analyzer_for("detected_candidate"),
                       r2=r2, **_commit_kwargs())
    assert out.action == "copy_failed"
    assert out.verify_ok is False


def test_analyzer_exception_quarantines() -> None:
    def boom(_k): raise ValueError("corrupt clip")
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=boom, r2=FakeR2(), **_commit_kwargs())
    assert out.action == "quarantined"
    assert out.error_code == "exception:ValueError"
    assert not out.r2_write


# ---------- 감사 + 로그 안전 ----------

def test_audit_recorded_and_log_leaks_nothing() -> None:
    auditor = FakeAuditor()
    out = process_clip(SRC, mode=GmeMode.TEST_COPY, analyzer=analyzer_for("detected_candidate"),
                       r2=FakeR2(), auditor=auditor, **_commit_kwargs())
    assert out.db_write and len(auditor.records) == 1
    rec = auditor.records[0]
    assert rec["decision"] == "detected_candidate" and rec["destination"] == "A"
    assert rec["source_key"] == SRC              # 감사 레코드엔 추적용으로 있음
    # 로그 한 줄엔 경로·dst_key·clip id 새면 안 됨
    line = out.safe_log_line()
    assert SRC not in line and (out.dst_key or "") not in line
    assert "abcdef12" not in line
    assert "decision=detected_candidate" in line and "destination=A" in line


def test_no_production_write_ever() -> None:
    for mode in (GmeMode.OFF, GmeMode.DRY_RUN, GmeMode.TEST_COPY):
        out = process_clip(SRC, mode=mode, analyzer=analyzer_for("detected_candidate"),
                           r2=FakeR2(), **_commit_kwargs())
        assert out.production_write is False
