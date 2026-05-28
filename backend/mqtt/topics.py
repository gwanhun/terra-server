"""MQTT 토픽 패턴 상수.

토픽 규칙:
- `esp32/{device_id}/telemetry`   디바이스 → 서버 (QoS 0, 3초 주기)
- `esp32/{device_id}/command`     서버 → 디바이스 (QoS 1, retain=false 필수)
- `esp32/{device_id}/ack`         디바이스 → 서버 (QoS 1)
- `esp32/{device_id}/alert`       디바이스 → 서버 (QoS 1, 즉시 알림)

device_id 는 devices.device_id 컬럼 (ESP32 MQTT client_id 와 동일).
"""

from __future__ import annotations

TOPIC_PREFIX = "esp32"

TOPIC_TELEMETRY_SUB = f"{TOPIC_PREFIX}/+/telemetry"
TOPIC_ACK_SUB = f"{TOPIC_PREFIX}/+/ack"
TOPIC_ALERT_SUB = f"{TOPIC_PREFIX}/+/alert"


def topic_telemetry(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/telemetry"


def topic_command(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/command"


def topic_ack(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/ack"


def topic_alert(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/alert"


def parse_device_id(topic: str) -> str | None:
    """`esp32/{device_id}/{kind}` → device_id 추출. 패턴 안 맞으면 None."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != TOPIC_PREFIX:
        return None
    return parts[1]
