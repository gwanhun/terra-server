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
ACTION_REBOOT = "reboot"
REBOOT_TTL_SEC = 60


def rotation_command(rotate_180: bool) -> dict[str, Any]:
    """`esp32/{camera_id}/command` 용 set_rotation 페이로드."""
    return {
        "msg_id": str(uuid4()),
        "issued_at": int(time.time()),
        "ttl_sec": ROTATION_TTL_SEC,
        "action": ACTION_SET_ROTATION,
        "rotate_180": bool(rotate_180),
    }


def reboot_command() -> dict[str, Any]:
    """`esp32/{camera_id}/command` 용 reboot 페이로드.

    펌웨어(app_mqtt.c `reboot` action)는 ack 를 먼저 발행하고 1.5초 뒤 `SW:mqtt_reboot`
    사유로 재부팅한다. 하트비트는 살아 있는데 녹화·업로드가 멈춘 카메라를 서버가
    원격으로 되살리는 용도(2026-09-28 베타 멈춤 대응). WiFi 가 죽은 카메라에는 닿지
    않는다(그쪽은 펌웨어 net_wd/boot_wifi 재시도가 담당).
    """
    return {
        "msg_id": str(uuid4()),
        "issued_at": int(time.time()),
        "ttl_sec": REBOOT_TTL_SEC,
        "action": ACTION_REBOOT,
    }
