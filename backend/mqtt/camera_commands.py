"""
카메라 워커 설정 명령 페이로드 빌더.

라우터(PATCH /cameras/{id})와 MQTT 브리지(텔레메트리 동기화) 두 곳에서 같은 명령을
발행하므로 action 이름·필드가 어긋나지 않게 여기서만 만든다.

계약: firebeetle2-p4-yr030/docs/HANDOFF_REPLY_CAMERA_ROTATE180_2026-09-08.md §5,
      docs/MQTT.md §2 (카메라 action).
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

# 즉시성 명령. 펌웨어는 SNTP 동기 시 now - issued_at > ttl_sec 이면 거부한다.
ROTATION_TTL_SEC = 60

ACTION_SET_ROTATION = "set_rotation"


def rotation_command(rotate_180: bool) -> dict[str, Any]:
    """`esp32/{camera_id}/command` 용 set_rotation 페이로드."""
    return {
        "msg_id": str(uuid4()),
        "issued_at": int(time.time()),
        "ttl_sec": ROTATION_TTL_SEC,
        "action": ACTION_SET_ROTATION,
        "rotate_180": bool(rotate_180),
    }
