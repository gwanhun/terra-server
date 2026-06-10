"""MQTT-backed WebRTC signaling helpers.

The REST API uses this module to relay SDP/ICE messages to a paired camera
worker over the existing Mosquitto command topic.  Video media itself does not
flow through terra-server; this is signaling only.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import ssl
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from backend.mqtt.topics import topic_ack, topic_command

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


class WebRTCSignalingError(RuntimeError):
    """Base error for MQTT signaling failures."""


class WebRTCSignalingTimeout(WebRTCSignalingError):
    """Raised when a camera does not answer within the REST timeout."""


class MqttWebRTCSignaling:
    """Small one-shot MQTT client for camera WebRTC signaling.

    The main bridge process already owns long-lived MQTT subscriptions for
    telemetry/ack persistence.  API requests need a synchronous request/reply
    shape, so each offer uses a short-lived client subscribed to that camera's
    ack topic before publishing the command.
    """

    def __init__(self) -> None:
        load_dotenv(REPO_ROOT / '.env')
        self._broker_host = os.getenv('MQTT_BROKER_HOST', 'localhost')
        self._broker_port = int(os.getenv('MQTT_BROKER_PORT', '8883'))
        self._username = os.getenv('MQTT_BRIDGE_USERNAME', '')
        self._password = os.getenv('MQTT_BRIDGE_PASSWORD', '')
        self._ca_cert_path = os.getenv('MQTT_CA_CERT_PATH', '').strip() or None
        self._use_tls = os.getenv('MQTT_USE_TLS', 'true').lower() == 'true'

        if not self._username or not self._password:
            raise WebRTCSignalingError(
                'MQTT_BRIDGE_USERNAME / MQTT_BRIDGE_PASSWORD is required for WebRTC signaling.'
            )

    def _new_client(self) -> mqtt.Client:
        client_id = f"terra-api-webrtc-{secrets.token_hex(4)}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        client.username_pw_set(self._username, self._password)
        if self._use_tls:
            client.tls_set(
                ca_certs=self._ca_cert_path,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        return client

    def request_answer(
        self,
        camera_id: str,
        command: dict[str, Any],
        *,
        session_id: str,
        timeout_sec: float,
    ) -> dict[str, Any]:
        """Publish a WebRTC offer and wait for a matching answer ack."""
        client = self._new_client()
        ack_topic = topic_ack(camera_id)
        cmd_topic = topic_command(camera_id)
        done = threading.Event()
        subscribed = threading.Event()
        result: dict[str, Any] = {}
        errors: list[str] = []

        def on_connect(
            c: mqtt.Client,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _props: Any = None,
        ) -> None:
            if reason_code != 0:
                errors.append(f'MQTT connect failed: {reason_code}')
                done.set()
                return
            c.subscribe(ack_topic, qos=1)

        def on_subscribe(
            c: mqtt.Client,
            _userdata: Any,
            _mid: int,
            _reason_codes: Any,
            _props: Any = None,
        ) -> None:
            subscribed.set()
            info = c.publish(
                cmd_topic,
                payload=json.dumps(command),
                qos=1,
                retain=False,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                errors.append(f'MQTT publish failed: {info.rc}')
                done.set()

        def on_message(_c: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
            try:
                payload = json.loads(msg.payload.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning('WebRTC ack parse failed: %s', exc)
                return

            if payload.get('session_id') != session_id:
                return

            action = payload.get('action') or payload.get('type')
            has_answer = payload.get('sdp') or isinstance(payload.get('answer'), dict)
            if action == 'webrtc_answer' or has_answer:
                result.update(payload)
                done.set()

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message

        try:
            client.connect(self._broker_host, self._broker_port, keepalive=30)
            client.loop_start()
            if not done.wait(timeout_sec):
                if not subscribed.is_set():
                    raise WebRTCSignalingTimeout('camera ack topic subscription timed out')
                raise WebRTCSignalingTimeout('camera WebRTC answer timed out')
            if errors:
                raise WebRTCSignalingError(errors[0])
            return result
        finally:
            client.loop_stop()
            client.disconnect()

    def publish(self, camera_id: str, command: dict[str, Any], *, timeout_sec: float = 5.0) -> None:
        """Publish a fire-and-forget WebRTC command."""
        client = self._new_client()
        topic = topic_command(camera_id)
        try:
            client.connect(self._broker_host, self._broker_port, keepalive=30)
            client.loop_start()
            info = client.publish(
                topic,
                payload=json.dumps(command),
                qos=1,
                retain=False,
            )
            if not info.wait_for_publish(timeout=timeout_sec):
                raise WebRTCSignalingTimeout('MQTT publish timed out')
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise WebRTCSignalingError(f'MQTT publish failed: {info.rc}')
        finally:
            client.loop_stop()
            client.disconnect()
