"""온도 보정 분리 — 공장 기본(base)과 사용자 보정값 사이 변환, 한 곳 (Stage S).

기기는 보정값을 **절대값 하나**만 가진다(NVS `terra/t_off`, 없으면 빌드 기본 -1.5℃). 앱이 보는 값은
"사용자 보정값"이어야 하고(-1.5 는 앱에 안 보임), 둘 사이 변환은 서버 브리지의 경계 두 곳에서만 한다:
- 발행 직전(dispatcher): 기기에 갈 `offset_c` = 사용자값 + base
- telemetry 수신(handlers): `devices.temp_offset_user_c` = 기기 보고값 − base

앱 0.166.0~ 이 이미 합산값을 `offset_c` 로 보내고 `devices.temp_offset_c` 를 절대값으로 읽는 버전이
배포돼 있어 **기존 키·컬럼 의미는 그대로**고, 새 키 `user_offset_c`·새 컬럼 `temp_offset_user_c` 만 추가한다
(specs/stage-s-temp-offset-base.md D2·D4). 변환이 흩어지면 이중 합산이 생기므로 API·콘솔·앱은 이 모듈 밖에서
더하거나 빼지 않는다.

base 는 env `TEMP_OFFSET_BASE_C`(기본 -1.5). 펌웨어 빌드 기본(`CONFIG_APP_TEMP_OFFSET_MDEG`)과 같아야 한다 —
다르면 flash erase 뒤 기기가 보고한 -1.5 가 사용자값 0 이 아닌 값으로 기록된다. 비어 있거나 숫자가 아니면
0 이 아니라 **기본값**으로 떨어진다: 0 이면 신 앱 사용자 기기가 1.5℃ 높게 읽혀 v4 29℃ 경고가 잘못 나간다.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_C = -1.5
DEVICE_LIMIT_C = 10.0            # 펌웨어 set_temp_offset 허용 범위 ±10 (docs/MQTT.md §2)
ENV_VAR = "TEMP_OFFSET_BASE_C"
USER_KEY = "user_offset_c"       # 신 앱: 사용자 보정값
DEVICE_KEY = "offset_c"          # 구 앱·콘솔: 기기에 갈 절대값(합산값). 그대로 통과

RESULT_INVALID = "invalid_payload"
RESULT_OUT_OF_RANGE = "offset_out_of_range"

_warned_bad_env = False


class OffsetError(ValueError):
    """payload 의 보정값이 잘못됐다. `result` 는 commands.result 에 그대로 쓰는 값."""

    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


def base_c(device_uuid: str | None = None) -> float:
    """공장 기본 보정(℃). 지금은 전 기기 공통 env 값 — 보드별로 달라지면 device_uuid 로 분기(D1)."""
    global _warned_bad_env
    raw = (os.getenv(ENV_VAR) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if raw and not _warned_bad_env:
        _warned_bad_env = True
        logger.warning("%s=%r 숫자 아님 — 기본 %.1f 사용", ENV_VAR, raw, DEFAULT_BASE_C)
    return DEFAULT_BASE_C


def to_device(user_c: float, device_uuid: str | None = None) -> float:
    """사용자 보정값 → 기기에 보낼 절대값. 기기값·발행값은 2자리(기존 temp_offset_c 와 동일)."""
    return round(float(user_c) + base_c(device_uuid), 2)


def to_user(device_c: float, device_uuid: str | None = None) -> float:
    """기기 보고 절대값 → 사용자 보정값. 앱이 0.1 단위로 '저장 완료'를 비교하므로 1자리(D12)."""
    return round(float(device_c) - base_c(device_uuid), 1)


def user_range(device_uuid: str | None = None) -> tuple[float, float]:
    """사용자값 허용 범위 = 기기 ±10 에서 base 를 뺀 것. 지금 -8.5 ~ +11.5. 문구에 쓸 때도 이걸로(하드코딩 금지)."""
    b = base_c(device_uuid)
    return (round(-DEVICE_LIMIT_C - b, 1), round(DEVICE_LIMIT_C - b, 1))


def device_in_range(device_c: float) -> bool:
    return -DEVICE_LIMIT_C <= device_c <= DEVICE_LIMIT_C


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve_device_offset(payload: Any, device_uuid: str | None = None) -> float | None:
    """`set_temp_offset` payload 에서 기기에 보낼 `offset_c` 를 정한다.

    - `user_offset_c` 없음 → None (구 경로: `offset_c` 그대로, 호출자가 건드리지 않음)
    - `user_offset_c` 있음 → 합산한 기기값. 두 키 동시·숫자 아님 → OffsetError(invalid_payload),
      합산 결과 ±10 밖 → OffsetError(offset_out_of_range)
    dispatcher(발행 직전)와 API(사전 검증)가 같은 함수를 써서 판정이 갈라지지 않게 한다.
    """
    if not isinstance(payload, dict) or USER_KEY not in payload:
        return None
    if DEVICE_KEY in payload:
        raise OffsetError(RESULT_INVALID, f"{USER_KEY} 와 {DEVICE_KEY} 는 함께 보낼 수 없음")
    v = payload.get(USER_KEY)
    if not _is_number(v):
        raise OffsetError(RESULT_INVALID, f"{USER_KEY} 는 숫자여야 함")
    dev = to_device(v, device_uuid)
    if not device_in_range(dev):
        lo, hi = user_range(device_uuid)
        raise OffsetError(RESULT_OUT_OF_RANGE, f"{USER_KEY} 는 {lo:g} ~ {hi:g} 범위")
    return dev


__all__ = [
    "DEFAULT_BASE_C", "DEVICE_LIMIT_C", "ENV_VAR", "USER_KEY", "DEVICE_KEY",
    "RESULT_INVALID", "RESULT_OUT_OF_RANGE", "OffsetError",
    "base_c", "to_device", "to_user", "user_range", "device_in_range", "resolve_device_offset",
]
