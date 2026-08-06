"""런타임 핀 가드 테스트 — fail-closed."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.gme import PINNED_GME_COMMIT
from backend.gme.pin import (
    CANONICAL_PIN,
    PinMismatch,
    observed_from_result,
    verify_pin,
)


@dataclass
class _Result:
    checkpoint_sha256: str = CANONICAL_PIN.checkpoint_sha256
    schema_version: str = CANONICAL_PIN.schema_version
    model_name: str = CANONICAL_PIN.model_name
    algorithm_version: str = CANONICAL_PIN.algorithm_version
    engine_schema_version: str = CANONICAL_PIN.engine_schema_version
    threshold: float = CANONICAL_PIN.threshold


def _observed(**over):
    r = _Result()
    obs = observed_from_result(r, runtime_commit=PINNED_GME_COMMIT)
    obs.update(over)
    return obs


def test_matching_pin_passes() -> None:
    verify_pin(_observed())  # 예외 없어야


def test_canonical_pin_matches_session_values() -> None:
    assert CANONICAL_PIN.commit == PINNED_GME_COMMIT
    assert CANONICAL_PIN.checkpoint_sha256.startswith("cd1162b4")
    assert CANONICAL_PIN.threshold == 0.5


@pytest.mark.parametrize(
    "over",
    [
        {"commit": "deadbeef"},
        {"checkpoint_sha256": "0" * 64},
        {"threshold": 0.4},
        {"schema_version": "gme-presence-v2"},
        {"model_name": "rf-detr-small"},
        {"algorithm_version": "gme-motion-v1"},
        {"engine_schema_version": "gme-shadow-v2"},
        {"commit": None},                    # 누락도 mismatch (fail-closed)
        {"threshold": None},
    ],
)
def test_any_mismatch_raises(over) -> None:
    with pytest.raises(PinMismatch):
        verify_pin(_observed(**over))


def test_missing_runtime_commit_fails_closed() -> None:
    r = _Result()
    obs = observed_from_result(r, runtime_commit=None)  # worker 가 commit 안 줌
    with pytest.raises(PinMismatch):
        verify_pin(obs)
