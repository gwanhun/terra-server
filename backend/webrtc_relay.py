"""
WebRTC ICE candidate 릴레이 (펌웨어 → 웹 방향).

## 왜 필요한가
[backend/webrtc_signaling.py] 의 시그널링은 단방향:
  웹 → POST /webrtc/ice → MQTT command → 펌웨어  (이 방향만 있었음)

펌웨어 esp_webrtc 는 보통 trickle 모드라 ICE candidate 를 SDP answer 와 별도로 만든다.
그걸 웹에 전달할 채널이 없으면 ICE 페어링 실패 → PeerConnection failed.

## 동작
1. terra-api 시작 시 1개의 long-lived paho-mqtt 클라이언트가 `esp32/+/ack` 구독.
2. 메시지 중 `action == "webrtc_ice"` 인 것만 추출 → session_id 별 buffer 에 append.
3. `GET /cameras/{uuid}/webrtc/candidates?session_id=...&since_index=N` (long-poll) 가
   buffer 에서 since_index 이후의 candidate 들을 반환. 없으면 짧게 대기(asyncio).
4. 세션 close 시 buffer drop (메모리 누수 방지).

## 다른 ack subscriber 와 충돌?
- terra-bridge 의 ack handler 도 같은 토픽 구독 중. MQTT 는 multi-subscriber 정상.
- 우리는 `action == "webrtc_ice"` 만 봄. bridge 는 commands 매칭만 함. 책임 안 겹침.

## 펌웨어가 publish 해야 할 메시지 (참고)
    topic:   esp32/{camera_id}/ack
    payload: { "action": "webrtc_ice", "session_id": "...", "candidate": { ... RTCIceCandidateInit ... } }

`candidate` 필드는 `RTCIceCandidate.toJSON()` 결과 형태 (`candidate`, `sdpMid`, `sdpMLineIndex` 키).
"""

from __future__ import annotations

import asyncio
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

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

# session 당 buffer 가 너무 커지지 않도록 안전망 — ICE candidate 수십 개를 넘기 어려움.
_MAX_CANDIDATES_PER_SESSION = 256


class IceRelay:
    """
    `esp32/+/ack` 를 listen 하면서 webrtc_ice 페이로드를 session 별 buffer 에 누적.
    long-poll API 가 buffer 를 polling 으로 읽음.

    싱글톤 의도 — terra-api 프로세스 당 1개 인스턴스 (`get_relay()` 통해 접근).
    """

    def __init__(self) -> None:
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._lock = threading.Lock()
        self._started = False

    # ---------- lifecycle ----------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """FastAPI lifespan startup 에서 호출.

        loop 는 candidate 도착 시 asyncio.Event.set() 을 콜백으로 호출하기 위해 필요
        (paho 콜백은 별도 스레드라 직접 set 호출 불가).
        """
        if self._started:
            return
        load_dotenv(REPO_ROOT / ".env")
        host = os.getenv("MQTT_BROKER_HOST", "localhost")
        port = int(os.getenv("MQTT_BROKER_PORT", "8883"))
        username = os.getenv("MQTT_BRIDGE_USERNAME", "")
        password = os.getenv("MQTT_BRIDGE_PASSWORD", "")
        ca_cert = os.getenv("MQTT_CA_CERT_PATH", "").strip() or None
        use_tls = os.getenv("MQTT_USE_TLS", "true").lower() == "true"

        if not username or not password:
            logger.warning(
                "IceRelay: MQTT_BRIDGE_USERNAME/PASSWORD 비어있음 — ICE 릴레이 비활성화"
            )
            return

        client_id = f"terra-api-icerelay-{secrets.token_hex(4)}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        client.username_pw_set(username, password)
        if use_tls:
            client.tls_set(ca_certs=ca_cert, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        self._client = client
        self._loop = loop
        try:
            client.connect_async(host, port, keepalive=30)
            client.loop_start()
            self._started = True
            logger.info("IceRelay: 시작 (broker=%s:%d, client_id=%s)", host, port, client_id)
        except Exception:  # noqa: BLE001
            logger.exception("IceRelay: MQTT connect 실패")
            self._client = None

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("IceRelay: shutdown 중 예외")
            self._client = None
        self._started = False
        with self._lock:
            self._buffers.clear()
            self._events.clear()

    # ---------- buffer API ----------

    def drop_session(self, session_id: str) -> None:
        """세션 종료 시 buffer/event 정리."""
        with self._lock:
            self._buffers.pop(session_id, None)
            evt = self._events.pop(session_id, None)
        if evt is not None:
            # 대기 중인 long-poll 깨워서 빈 결과 반환하게 함
            self._signal_event(evt)

    async def wait_for_candidates(
        self,
        session_id: str,
        since_index: int,
        timeout_sec: float,
    ) -> list[dict[str, Any]]:
        """since_index 이후의 candidate 들을 반환. 없으면 timeout_sec 까지 대기."""
        end_time = time.monotonic() + timeout_sec
        while True:
            with self._lock:
                buf = self._buffers.get(session_id, [])
                if since_index < len(buf):
                    return list(buf[since_index:])
                # 아직 새 candidate 없음 → 이 세션용 event 준비
                evt = self._events.get(session_id)
                if evt is None:
                    evt = asyncio.Event()
                    self._events[session_id] = evt

            remaining = end_time - time.monotonic()
            if remaining <= 0:
                return []
            try:
                await asyncio.wait_for(evt.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return []
            # 깨어났으면 다시 buffer 확인 (concurrent 추가 가능)
            # 다음 wait 위해 event 새로 만듦
            with self._lock:
                self._events[session_id] = asyncio.Event()

    # ---------- paho callbacks (별도 스레드) ----------

    def _on_connect(self, client: mqtt.Client, _ud: Any, _flags: Any,
                    reason_code: Any, _props: Any = None) -> None:
        if reason_code != 0:
            logger.error("IceRelay: MQTT connect 실패 reason=%s", reason_code)
            return
        client.subscribe("esp32/+/ack", qos=1)
        logger.info("IceRelay: esp32/+/ack 구독")

    def _on_disconnect(self, _client: mqtt.Client, _ud: Any, _flags: Any,
                       reason_code: Any, _props: Any = None) -> None:
        # paho 의 loop_start 자체가 자동 reconnect 처리
        logger.warning("IceRelay: MQTT disconnect reason=%s — auto-reconnect 대기", reason_code)

    def _on_message(self, _client: mqtt.Client, _ud: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("action") != "webrtc_ice":
            return
        session_id = payload.get("session_id")
        candidate = payload.get("candidate")
        if not session_id or not isinstance(candidate, dict):
            return

        evt: asyncio.Event | None = None
        with self._lock:
            buf = self._buffers.setdefault(session_id, [])
            if len(buf) >= _MAX_CANDIDATES_PER_SESSION:
                logger.warning("IceRelay: session=%s buffer 가득 — 신규 candidate 드롭", session_id)
                return
            buf.append(candidate)
            evt = self._events.get(session_id)

        if evt is not None:
            self._signal_event(evt)

    # ---------- helpers ----------

    def _signal_event(self, evt: asyncio.Event) -> None:
        """다른 스레드에서 asyncio.Event.set 을 안전하게 호출."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(evt.set)


# 싱글톤 인스턴스 — main.py lifespan 이 start/stop 호출.
_relay = IceRelay()


def get_relay() -> IceRelay:
    return _relay
