"""3상태 presence 판정 → 저장 목적지 라우팅. 순수 함수, 부수효과 없음.

계약(gecko-vision-gate `gme_presence` 와 합의):
- detected_candidate → A               (게코 후보 직접 관측)
- not_observed       → B_REVIEW        (현재 모델이 관측 못함 · 부재 확정 아님)
- unresolved         → RETRY_OR_QUARANTINE (실패·판단 불충분 · **절대 B 아님**)

fail-closed 원칙: 알 수 없거나 비정상 판정값은 A/B 로 보내지 않고 격리한다.
not_observed 를 부재 확정으로, unresolved 를 B 로 오분류하지 않게 하는 것이 핵심.
"""

from __future__ import annotations

from typing import Final

# 저장 목적지 라벨 (실제 R2 경로는 이후 write 단계에서 결정)
DEST_A: Final = "A"
DEST_B_REVIEW: Final = "B_REVIEW"
DEST_RETRY_OR_QUARANTINE: Final = "RETRY_OR_QUARANTINE"

# 유효 판정값 → 목적지. 이 dict 에 없는 값은 전부 격리(fail-closed).
_DECISION_TO_DESTINATION: Final[dict[str, str]] = {
    "detected_candidate": DEST_A,
    "not_observed": DEST_B_REVIEW,
    "unresolved": DEST_RETRY_OR_QUARANTINE,
}

# B_REVIEW 로 갈 수 있는 유일한 판정. 이 외에는 어떤 것도 B 로 라우팅되면 안 된다.
_ONLY_DECISION_ALLOWED_TO_B: Final = "not_observed"


def route_decision(decision: str | None) -> str:
    """presence 판정 문자열을 저장 목적지 라벨로 변환한다.

    미지/None/비정상 값은 RETRY_OR_QUARANTINE 로 격리한다 (fail-closed).
    """
    return _DECISION_TO_DESTINATION.get(decision or "", DEST_RETRY_OR_QUARANTINE)


def is_b_review(destination: str) -> bool:
    return destination == DEST_B_REVIEW
