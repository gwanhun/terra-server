"""런타임 핀 가드 — 로드된 GME 정체성이 승인된 핀과 정확히 일치하는지 검증.

commit·schema·algorithm·checkpoint SHA·threshold 중 **하나라도** 어긋나면 PinMismatch
(fail-closed). 검증에 필요한 관측값이 누락돼도 mismatch 로 처리한다(안전측).

commit 은 PresenceResult 에 없다(=설치된 gecko-vision-gate 패키지의 git rev).
worker 가 자기 env 의 설치 rev 를 runtime_commit 으로 넘겨줘야 한다. terra-server
API 는 모델을 안 돌리므로 이 가드는 worker 컨텍스트에서 실행된다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from . import PINNED_GME_COMMIT


class PinMismatch(RuntimeError):
    """로드된 GME 정체성이 핀과 불일치 (fail-closed)."""


@dataclass(frozen=True, slots=True)
class ExpectedPin:
    commit: str
    checkpoint_sha256: str
    threshold: float
    schema_version: str
    model_name: str
    algorithm_version: str
    engine_schema_version: str


# 이번 세션에서 검증된 승인 핀 (gecko-vision-gate 077bed1 + v2 checkpoint).
CANONICAL_PIN = ExpectedPin(
    commit=PINNED_GME_COMMIT,
    checkpoint_sha256="cd1162b4c95041bc9b1ec064bb82ff67cb7d7416b2c778230ea0a59e2f2bef17",
    threshold=0.5,
    schema_version="gme-presence-v1",
    model_name="rf-detr-nano",
    algorithm_version="gme-motion-v0",
    engine_schema_version="gme-shadow-v1",
)

# 관측값에서 확인하는 필드 (commit 은 worker 가 별도 주입).
_STR_FIELDS = (
    "checkpoint_sha256",
    "schema_version",
    "model_name",
    "algorithm_version",
    "engine_schema_version",
    "commit",
)


def verify_pin(observed: Mapping[str, object], expected: ExpectedPin = CANONICAL_PIN) -> None:
    """관측 정체성이 핀과 일치하는지 검증. 불일치·누락 시 PinMismatch (fail-closed)."""
    mismatches: list[str] = []

    for field in _STR_FIELDS:
        exp = getattr(expected, field)
        act = observed.get(field)
        if act != exp:
            # 값 자체는 노출하지 않고 필드명만 (checkpoint SHA·경로 유출 방지)
            mismatches.append(field)

    # threshold 는 float 비교 (허용오차)
    thr = observed.get("threshold")
    if not isinstance(thr, (int, float)) or isinstance(thr, bool) or not math.isclose(
        float(thr), expected.threshold, rel_tol=0.0, abs_tol=1e-9
    ):
        mismatches.append("threshold")

    if mismatches:
        raise PinMismatch(f"GME 핀 불일치 필드: {sorted(mismatches)}")


def observed_from_result(result: object, *, runtime_commit: str | None) -> dict[str, object]:
    """PresenceResult + worker 가 준 설치 commit 으로 관측 정체성 dict 구성."""
    return {
        "commit": runtime_commit,
        "checkpoint_sha256": getattr(result, "checkpoint_sha256", None),
        "schema_version": getattr(result, "schema_version", None),
        "model_name": getattr(result, "model_name", None),
        "algorithm_version": getattr(result, "algorithm_version", None),
        "engine_schema_version": getattr(result, "engine_schema_version", None),
        "threshold": getattr(result, "threshold", None),
    }
