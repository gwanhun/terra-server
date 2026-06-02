"""
MQTT 브리지 — Mosquitto ↔ Supabase 양방향 다리.

## 동작
1. Mosquitto 에 paho-mqtt 로 connect (TLS, username/password)
2. 다음 토픽 subscribe:
   - `esp32/+/telemetry` → telemetry_handler
   - `esp32/+/ack`       → ack_handler
   - `esp32/+/alert`     → alert_handler
3. Supabase commands 테이블 Realtime 구독:
   - status='pending' INSERT 감지 → MQTT publish → status='sent'
4. 재연결: paho-mqtt 자동 재연결 (지수 백오프)

## 왜 단일 프로세스?
브로커 인증, 토픽 라우팅, DB 연결을 한 프로세스에 모아 두면 디버깅 쉬움.
부하 늘면 telemetry/command 를 분리할 수 있지만 초기엔 통합.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from backend.mqtt import handlers
from backend.mqtt.dispatcher import CommandDispatcher
from backend.mqtt.topics import (
    TOPIC_ACK_SUB,
    TOPIC_ALERT_SUB,
    TOPIC_TELEMETRY_SUB,
    parse_device_id,
    topic_command,
)
from backend.supabase_client import get_supabase_client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)


class MqttBridge:
    """MQTT ↔ Supabase 브리지. start() 호출 후 stop() 까지 백그라운드 스레드 가동."""

    def __init__(self) -> None:
        load_dotenv(REPO_ROOT / ".env")
        self._broker_host = os.getenv("MQTT_BROKER_HOST", "localhost")
        self._broker_port = int(os.getenv("MQTT_BROKER_PORT", "8883"))
        self._username = os.getenv("MQTT_BRIDGE_USERNAME", "")
        self._password = os.getenv("MQTT_BRIDGE_PASSWORD", "")
        self._ca_cert_path = os.getenv("MQTT_CA_CERT_PATH", "").strip() or None
        self._use_tls = os.getenv("MQTT_USE_TLS", "true").lower() == "true"

        if not self._username or not self._password:
            raise RuntimeError(
                "MQTT_BRIDGE_USERNAME / MQTT_BRIDGE_PASSWORD 가 비어있음. .env 확인."
            )

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="terra-bridge",
            clean_session=True,
        )
        self._client.username_pw_set(self._username, self._password)
        if self._use_tls:
            self._client.tls_set(
                ca_certs=self._ca_cert_path,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._sb = get_supabase_client()
        self._stop_event = threading.Event()
        self._dispatcher = CommandDispatcher(self)

    # ---------- 라이프사이클 ----------

    def start(self) -> None:
        logger.info(
            "MQTT 브리지 시작: %s:%d (TLS=%s)",
            self._broker_host, self._broker_port, self._use_tls,
        )
        self._client.connect_async(self._broker_host, self._broker_port, keepalive=60)
        self._client.loop_start()
        self._dispatcher.start()

    def stop(self) -> None:
        logger.info("MQTT 브리지 정지")
        self._stop_event.set()
        self._dispatcher.stop()
        self._client.loop_stop()
        self._client.disconnect()

    def wait_stopped(self) -> None:
        """SIGTERM 등으로 stop() 호출 전까지 블로킹."""
        try:
            while not self._stop_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()

    # ---------- paho 콜백 ----------

    def _on_connect(self, client: mqtt.Client, _userdata: Any,
                    _flags: Any, reason_code: Any, _props: Any = None) -> None:
        if reason_code != 0:
            logger.error("MQTT 연결 실패: reason=%s", reason_code)
            return
        logger.info("MQTT 연결됨, 토픽 subscribe")
        client.subscribe([
            (TOPIC_TELEMETRY_SUB, 0),
            (TOPIC_ACK_SUB, 1),
            (TOPIC_ALERT_SUB, 1),
        ])

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any,
                       _flags: Any, reason_code: Any, _props: Any = None) -> None:
        logger.warning("MQTT 끊김: reason=%s — 자동 재연결 대기", reason_code)

    def _on_message(self, _client: mqtt.Client, _userdata: Any,
                    msg: mqtt.MQTTMessage) -> None:
        device_id = parse_device_id(msg.topic)
        if not device_id:
            logger.warning("토픽 패턴 불일치: %s", msg.topic)
            return

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("페이로드 JSON 파싱 실패 (topic=%s): %s", msg.topic, exc)
            return

        if msg.topic.endswith("/telemetry"):
            self._handle_telemetry(device_id, payload)
        elif msg.topic.endswith("/ack"):
            self._handle_ack(device_id, payload)
        elif msg.topic.endswith("/alert"):
            self._handle_alert(device_id, payload)

    # ---------- 핸들러 (handlers.py 로 위임) ----------

    def _handle_telemetry(self, device_id: str, payload: dict[str, Any]) -> None:
        logger.debug("telemetry from %s: %s", device_id, payload)
        handlers.handle_telemetry(device_id, payload)

    def _handle_ack(self, device_id: str, payload: dict[str, Any]) -> None:
        logger.info("ack from %s: %s", device_id, payload)
        handlers.handle_ack(device_id, payload)

    def _handle_alert(self, device_id: str, payload: dict[str, Any]) -> None:
        logger.warning("alert from %s: %s", device_id, payload)
        handlers.handle_alert(device_id, payload)

    # ---------- 명령 발행 ----------

    def publish_command(self, device_id: str, payload: dict[str, Any]) -> bool:
        """commands 테이블에서 호출됨. retain=false 필수."""
        topic = topic_command(device_id)
        info = self._client.publish(
            topic,
            payload=json.dumps(payload),
            qos=1,
            retain=False,
        )
        return info.rc == mqtt.MQTT_ERR_SUCCESS
