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
from typing import Any

logger = logging.getLogger(__name__)

# 명령 TTL 기본값 (dispatcher.DEFAULT_TTL_SEC 와 동일 의도 — 액추에이터는 짧게)
DEFAULT_CMD_TTL_SEC = 10

# 물분무 허용 지속시간 (ms). 앱 1/2/3초 버튼과 대응.
MIST_ACTION = "mist"
ALLOWED_MIST_MS: tuple[int, ...] = (1000, 2000, 3000)


class InvalidCommand(ValueError):
    """명령 검증 실패 — 라우터에서 400 으로 변환."""


def validate_mist_duration(duration_ms: Any) -> int:
    """물분무 지속시간 검증. 허용값 {1000,2000,3000} 외는 InvalidCommand."""
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
) -> dict[str, Any] | None:
    """`commands` 에 status='pending' 1건 INSERT. 삽입된 row 반환 (실패 시 None).

    issued_by 는 사용자 발행이면 owner_id, 시스템(스케줄) 발행도 owner_id 를 넣어
    앱의 명령 이력에서 본인 것으로 보이게 한다 (NULL 도 허용되지만 UX 상 owner 명시).

    source: 'manual' | 'schedule' | 'timer' | 'guard' — 감사 로그 출처 구분 (요청 5).
    source_id: 연결된 schedules.id 등. reason: 가드 사유 등.
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
