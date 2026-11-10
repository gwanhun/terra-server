"""온도 보정 분리 — 공장 기본(base)과 사용자 보정값 사이 변환, 한 곳 (Stage S).

기기는 보정값을 **절대값 하나**만 가진다(NVS `terra/t_off`, 없으면 빌드 기본 -1.5℃). 앱이 보는 값은
"사용자 보정값"이어야 하고(-1.5 는 앱에 안 보임), 둘 사이 변환은 서버 브리지의 경계 두 곳에서만 한다:
- 발행 직전(dispatcher): 기기에 갈 `offset_c` = 사용자값 + base
- telemetry 수신(handlers): `devices.temp_offset_user_c` = 기기 보고값 − base

앱 0.166.0~ 이 이미 합산값을 `offset_c` 로 보내고 `devices.temp_offset_c` 를 절대값으로 읽는 버전이
배포돼 있어 **기존 키·컬럼 의미는 그대로**고, 새 키 `user_offset_c`·새 컬럼 `temp_offset_user_c` 만 추가한다
(specs/stage-s-temp-offset-base.md D2·D4). 변환이 흩어지면 이중 합산이 생기므로 API·콘솔·앱은 이 모듈 밖에서
더하거나 빼지 않는다.

base 는 **보드별**이다(Stage T, specs/stage-t-temp-offset-base-per-device.md): 기기가 NVS `terra/t_base` 에 갖고
telemetry `temp_offset_base_c` 로 보고하면 서버는 `devices.temp_offset_base_c` 에 저장하고 그 값을 쓴다. 운영자는
`set_temp_base` 명령으로 보드마다 다르게(-2, -3 …) 넣을 수 있고, 기기가 적용값도 같이 옮기므로 사용자값은 안 움직인다.
`base_c(device_uuid)` 는 프로세스 캐시 → devices 조회 → env 순으로 찾는다.

보고가 없는 구 펌웨어는 env `TEMP_OFFSET_BASE_C`(기본 -1.5)로 폴백한다. 펌웨어 빌드 기본(`CONFIG_APP_TEMP_OFFSET_MDEG`)과
같아야 한다 — 다르면 flash erase 뒤 기기가 보고한 -1.5 가 사용자값 0 이 아닌 값으로 기록된다. 비어 있거나 숫자가 아니면
0 이 아니라 **기본값**으로 떨어진다: 0 이면 신 앱 사용자 기기가 1.5℃ 높게 읽혀 v4 29℃ 경고가 잘못 나간다.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_C = -1.5
DEVICE_LIMIT_C = 10.0            # 펌웨어 set_temp_offset 허용 범위 ±10 (docs/MQTT.md §2)
ENV_VAR = "TEMP_OFFSET_BASE_C"
USER_KEY = "user_offset_c"       # 신 앱: 사용자 보정값
DEVICE_KEY = "offset_c"          # 구 앱·콘솔: 기기에 갈 절대값(합산값). 그대로 통과
BASE_KEY = "base_c"              # set_temp_base 명령 payload / telemetry 는 temp_offset_base_c
BASE_ACTION = "set_temp_base"    # 운영자: 보드 공장 기본 보정 설정(기기가 적용값도 같이 옮김)
BASE_COLUMN = "temp_offset_base_c"
BASE_CACHE_TTL_SEC = 60.0        # telemetry(3초)가 계속 갱신. 오프라인 기기는 만료 뒤 DB 재조회

RESULT_INVALID = "invalid_payload"
RESULT_OUT_OF_RANGE = "offset_out_of_range"

_warned_bad_env = False


class OffsetError(ValueError):
    """payload 의 보정값이 잘못됐다. `result` 는 commands.result 에 그대로 쓰는 값."""

    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


# device_uuid → (base 또는 None=미보고, 만료 monotonic). None 도 캐시해 구 펌웨어 telemetry 마다 DB 를 안 친다.
_base_cache: dict[str, tuple[float | None, float]] = {}


def _env_base_c() -> float:
    """env 폴백(구 펌웨어·미보고 기기). 전 기기 공통."""
    global _warned_bad_env
    raw_env = os.getenv(ENV_VAR)
    if raw_env is None:                 # 아예 안 설정 — 기본값, 조용히
        return DEFAULT_BASE_C
    raw = raw_env.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    if not _warned_bad_env:             # 설정했는데 빈 값·숫자 아님(.env 오타) — 한 번만 경고
        _warned_bad_env = True
        logger.warning("%s=%r 숫자 아님 — 기본 %.1f 사용", ENV_VAR, raw, DEFAULT_BASE_C)
    return DEFAULT_BASE_C


def remember_base(device_uuid: str, base: float | None) -> None:
    """기기가 보고한(또는 ACK 로 확정된) base 를 프로세스 캐시에. None = 보고 없음(구 펌웨어)."""
    _base_cache[device_uuid] = (base, time.monotonic() + BASE_CACHE_TTL_SEC)


def forget_base(device_uuid: str | None = None) -> None:
    if device_uuid is None:
        _base_cache.clear()
    else:
        _base_cache.pop(device_uuid, None)


def _lookup_base(device_uuid: str) -> float | None:
    """devices.temp_offset_base_c 1건 조회. 실패·컬럼 없음·NULL·범위 밖 → None(env 폴백). 예외는 밖으로 안 낸다."""
    try:
        from backend.supabase_client import get_supabase_client   # 지연 임포트 — 순수 변환 테스트가 DB 를 안 보게
        res = (get_supabase_client().table("devices").select(BASE_COLUMN)
               .eq("id", device_uuid).limit(1).execute())
        rows = res.data if isinstance(res.data, list) else []
        v = rows[0].get(BASE_COLUMN) if rows and isinstance(rows[0], dict) else None
    except Exception as exc:  # noqa: BLE001 — supabase-py 예외 타입 넓음. 폴백이 있으니 경고만
        logger.warning("devices.%s 조회 실패 (device=%s) — env 폴백: %s", BASE_COLUMN, device_uuid, exc)
        return None
    if _is_number(v) and device_in_range(float(v)):
        return float(v)
    return None


def base_c(device_uuid: str | None = None) -> float:
    """공장 기본 보정(℃). 보드별: 캐시 → devices.temp_offset_base_c → env(-1.5).

    device_uuid 없음(사전 검증 등)이나 미보고 기기는 env 값. 조회 결과(None 포함)는 TTL 캐시."""
    if device_uuid is None:
        return _env_base_c()
    hit = _base_cache.get(device_uuid)
    if hit is None or hit[1] <= time.monotonic():
        base = _lookup_base(device_uuid)
        remember_base(device_uuid, base)
    else:
        base = hit[0]
    return base if base is not None else _env_base_c()


def to_device(user_c: float, device_uuid: str | None = None) -> float:
    """사용자 보정값 → 기기에 보낼 절대값. 기기값·발행값은 2자리(기존 temp_offset_c 와 동일)."""
    return round(float(user_c) + base_c(device_uuid), 2)


def to_user(device_c: float, device_uuid: str | None = None, *, base: float | None = None) -> float:
    """기기 보고 절대값 → 사용자 보정값. 앱이 0.1 단위로 '저장 완료'를 비교하므로 1자리(D12).

    `base` 를 주면(같은 telemetry 에 실려 온 보고값) 그걸 쓴다 — DB 왕복 없이 쌍이 맞는다. -0.0 은 0.0 으로."""
    b = base if base is not None else base_c(device_uuid)
    return round(float(device_c) - b, 1) + 0.0


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


def resolve_base_payload(payload: Any) -> float:
    """`set_temp_base` payload 검증 → 기기에 그대로 나갈 `base_c`. 서버 변환 없음(기기가 적용값을 같이 옮긴다).

    숫자 아님·키 없음 → OffsetError(invalid_payload), ±10 밖 → OffsetError(offset_out_of_range).
    적용값까지 옮긴 결과가 ±10 을 넘는지는 기기만 알아 기기가 거절한다(ack result)."""
    v = payload.get(BASE_KEY) if isinstance(payload, dict) else None
    if not _is_number(v):
        raise OffsetError(RESULT_INVALID, f"{BASE_ACTION} 은 payload.{BASE_KEY}(숫자) 필요")
    b = round(float(v), 2)
    if not device_in_range(b):
        raise OffsetError(RESULT_OUT_OF_RANGE, f"{BASE_KEY} 는 ±{DEVICE_LIMIT_C:g} 범위")
    return b


__all__ = [
    "DEFAULT_BASE_C", "DEVICE_LIMIT_C", "ENV_VAR", "USER_KEY", "DEVICE_KEY", "BASE_KEY", "BASE_ACTION", "BASE_COLUMN",
    "RESULT_INVALID", "RESULT_OUT_OF_RANGE", "OffsetError",
    "base_c", "remember_base", "forget_base", "to_device", "to_user", "user_range", "device_in_range",
    "resolve_device_offset", "resolve_base_payload",
]
