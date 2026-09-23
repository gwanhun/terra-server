"""
명령 생성 서비스 — Stage H.

`commands` 테이블에 pending 명령을 INSERT 하는 단일 진입점. mist REST 라우터와
schedule_runner 가 공유한다 (앱 직접 INSERT 와 달리 서버측 검증을 태우는 경로).

발행 자체는 기존 CommandDispatcher (dispatcher.py) 가 담당 — 여기선 INSERT 만.

## 물분무(mist) 계약

action `"mist"`, payload `{"duration_ms": 1000|2000|3000}`.
펌프 ON 후 firmware 내부 one-shot 타이머로 정확히 duration 뒤 자동 OFF (단일 명령 자기완결).
firmware 가 `MIST_MAX_MS` 로 상한 clamp 하지만, 서버도 허용값만 통과시켜 이중 방어.
하드웨어(릴레이/MOSFET) 차이는 firmware 가 흡수 — 계약·백엔드는 하드웨어 무관.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# 명령 TTL 기본값 (dispatcher.DEFAULT_TTL_SEC 와 동일 의도 — 액추에이터는 짧게)
DEFAULT_CMD_TTL_SEC = 10

# 물분무 허용 지속시간 (ms). 앱 칩은 5초·10초 (2026-09-23 결정). 나머지는 호환용 —
# 저장된 옛 예약(1/2/3초)과 앱 0.131.0 의 7초 칩이 400 을 맞지 않게 그대로 받는다.
# 기기 상한(capabilities.mist_max_ms, 미보고=5000)을 넘는 값은 dispatcher 가 분할 발행한다.
MIST_ACTION = "mist"
ALLOWED_MIST_MS: tuple[int, ...] = (1000, 2000, 3000, 5000, 7000, 10000)
# 모든 펌웨어가 지원하는 상한. 펌웨어 MIST_MAX_MS 가 5000 이던 시절(~2026-09-23)의 값으로,
# capabilities.mist_max_ms 를 보고하지 않는 구 펌웨어는 이 값으로 취급한다.
MIST_BASE_MAX_MS = 5000


class InvalidCommand(ValueError):
    """명령 검증 실패 — 라우터에서 400 으로 변환."""


def mist_max_ms_of(capabilities: Any) -> int:
    """기기가 보고한 분무 1회 상한(ms). 미보고/비정상이면 MIST_BASE_MAX_MS.

    쓰임(2026-09-23): 펌웨어는 상한을 넘는 duration 을 **조용히 clamp** 한다(구 펌웨어 5초).
    그대로 보내면 10초 요청이 5초만 뿌려지고 사용자는 10초로 믿는다. 그래서 dispatcher 가
    이 상한 단위로 **나눠 보낸다**(10초 = 5초 + 5초) — 펌웨어 플래시 없이 10초를 채우는 유일한
    방법. 상한은 펌웨어가 telemetry/페어링의 capabilities.mist_max_ms 로 보고한다(handlers).
    """
    if isinstance(capabilities, dict):
        v = capabilities.get("mist_max_ms")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    return MIST_BASE_MAX_MS


def validate_mist_duration(duration_ms: Any) -> int:
    """물분무 지속시간 형식 검증. 화이트리스트 밖이면 InvalidCommand.

    기기 상한 초과는 여기서 막지 않는다 — dispatcher 가 분할 발행으로 채운다.
    """
    if duration_ms not in ALLOWED_MIST_MS:
        raise InvalidCommand(
            f"duration_ms 는 {ALLOWED_MIST_MS} 중 하나여야 함 (got={duration_ms!r})"
        )
    return int(duration_ms)


def insert_pending_command(
    sb: Any,
    *,
    device_uuid: str,
    action: str,
    payload: dict[str, Any] | None = None,
    issued_by: str | None = None,
    ttl_sec: int = DEFAULT_CMD_TTL_SEC,
    source: str = "manual",
    source_id: str | None = None,
    reason: str | None = None,
    issued_at: datetime | None = None,
) -> dict[str, Any] | None:
    """`commands` 에 status='pending' 1건 INSERT. 삽입된 row 반환 (실패 시 None).

    issued_by 는 사용자 발행이면 owner_id, 시스템(스케줄) 발행도 owner_id 를 넣어
    앱의 명령 이력에서 본인 것으로 보이게 한다 (NULL 도 허용되지만 UX 상 owner 명시).

    source: 'manual' | 'schedule' | 'timer' | 'guard' — 감사 로그 출처 구분 (요청 5).
    source_id: 연결된 schedules.id 등. reason: 가드 사유 등.
    issued_at: 미래면 **예약 발행** — dispatcher 가 그 시각까지 pending 으로 둔다(mist 분할 후속).
    """
    row: dict[str, Any] = {
        "device_id": device_uuid,
        "action": action,
        "payload": payload,
        "issued_by": issued_by,
        "ttl_sec": ttl_sec,
        "status": "pending",
        "source": source,
        "source_id": source_id,
        "reason": reason,
    }
    if issued_at is not None:
        row["issued_at"] = issued_at.isoformat()
    res = sb.table("commands").insert(row).execute()
    data = res.data or []
    if not data:
        logger.error("commands INSERT 실패 (device=%s action=%s)", device_uuid, action)
        return None
    return data[0]


__all__ = [
    "ALLOWED_MIST_MS",
    "DEFAULT_CMD_TTL_SEC",
    "InvalidCommand",
    "MIST_ACTION",
    "insert_pending_command",
    "validate_mist_duration",
]
