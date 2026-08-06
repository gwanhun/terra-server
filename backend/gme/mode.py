"""GME A/B 활성화 모드 (기능 플래그). 코드 배포와 실제 copy 활성화를 분리한다.

- off             : 아무 작업도 안 함. **운영 기본값.** 분석조차 하지 않음.
- dry_run         : GME 결과로 목적지만 계산, R2/DB write 없음.
- test_copy       : 승인된 test 전용 prefix 로만 원본 보존 복사.
- production_copy : **현재 구현·활성화 금지.** 설정돼도 파이프라인이 거부(fail-closed).

기본값은 반드시 off. 알 수 없는 값도 off 로 떨어진다(fail-safe).
"""

from __future__ import annotations

import os
from enum import Enum


class GmeMode(str, Enum):
    OFF = "off"
    DRY_RUN = "dry_run"
    TEST_COPY = "test_copy"
    PRODUCTION_COPY = "production_copy"


DEFAULT_MODE = GmeMode.OFF
_ENV_VAR = "GME_MODE"

# 현재 허용되는 모드 (production_copy 제외).
_ALLOWED = frozenset({GmeMode.OFF, GmeMode.DRY_RUN, GmeMode.TEST_COPY})


class GmeModeForbidden(RuntimeError):
    """production_copy 등 현재 금지된 모드 활성화 시도."""


def resolve_mode(env: dict[str, str] | None = None) -> GmeMode:
    """환경변수 GME_MODE 에서 모드 결정. 미설정/미지값은 off (fail-safe)."""
    raw = (env if env is not None else os.environ).get(_ENV_VAR, "").strip().lower()
    if not raw:
        return DEFAULT_MODE
    try:
        return GmeMode(raw)
    except ValueError:
        return DEFAULT_MODE


def assert_mode_allowed(mode: GmeMode) -> None:
    """금지 모드면 예외. production_copy 는 현재 활성화 불가."""
    if mode not in _ALLOWED:
        raise GmeModeForbidden(f"GME mode 활성화 금지: {mode.value}")


def writes_to_r2(mode: GmeMode) -> bool:
    """이 모드가 R2 에 실제 copy 를 하는가 (test_copy 만)."""
    return mode == GmeMode.TEST_COPY
