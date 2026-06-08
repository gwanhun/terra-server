"""
ESP32-S3 디바이스 시뮬레이터 (참조 구현).

[docs/FIRMWARE_INTEGRATION.md](../docs/FIRMWARE_INTEGRATION.md) 의 동작을 Python 으로 흉내냄.
펌웨어 AI 가 ESP-IDF 코드 짤 때 옆에 띄워두고 참조하는 ground truth.

## 동작
1. (옵션 --pair) HTTPS POST /devices/pair → device_id + mqtt_token 받아 .sim_state.json 저장
2. (옵션 안 줘도) .sim_state.json 에서 자격증명 로드
3. MQTT TLS 연결 (mqtt.terra-server.uk:8883)
4. 3초마다 가짜 telemetry publish (random 노이즈)
5. command 토픽 구독 → 수신 시 ack publish (heater_locked 시뮬레이션 포함)
6. (옵션) heater 90초 연속 가동 시 alert publish + latch

## 사용
    # 첫 페어링 (JWT 필요 — Supabase 로그인 후 access_token)
    uv run python scripts/sim_device.py --pair --jwt eyJhbGc... --name "시뮬"

    # 페어링 후 (자격증명 .sim_state.json 에 저장됨)
    uv run python scripts/sim_device.py

    # 다른 디바이스 ID 시뮬레이션 (별도 state 파일)
    uv run python scripts/sim_device.py --state-file .sim2.json --pair --jwt ...

## 종료
    Ctrl+C → 클린 disconnect

## 의존성 (이미 pyproject 에 있음)
    paho-mqtt, httpx (urllib 도 가능 — 단순화 위해 urllib 사용)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

logger = logging.getLogger("sim_device")

DEFAULT_API_BASE = "https://api.terra-server.uk"
DEFAULT_MQTT_HOST = "mqtt.terra-server.uk"
DEFAULT_MQTT_PORT = 8883
DEFAULT_STATE_FILE = ".sim_state.json"
TELEMETRY_INTERVAL_SEC = 3.0
HEATER_LATCH_SEC = 90.0   # 연속 가동 임계
MSG_ID_RINGBUFFER_SIZE = 8


# ─────────────────────────────────────────────────────────────────────────
# 1. 페어링 — HTTPS POST /devices/pair
# ─────────────────────────────────────────────────────────────────────────


def pair_device(api_base: str, jwt: str, name: str, species: str | None) -> dict[str, str]:
    """POST /devices/pair → {id, device_id, mqtt_token}.

    펌웨어의 cloud_client_pair() 와 동일한 흐름.
    """
    url = api_base.rstrip("/") + "/devices/pair"
    body = {"name": name, "firmware_ver": "sim-device 0.1.0"}
    if species:
        body["species"] = species

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"페어링 실패 HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"페어링 실패 (네트워크): {e}")

    logger.info("페어링 OK — device_id=%s", data["device_id"])
    return data


# ─────────────────────────────────────────────────────────────────────────
# 2. NVS 시뮬레이션 — .sim_state.json 에 자격증명 저장/로드
# ─────────────────────────────────────────────────────────────────────────


def load_state(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def save_state(path: Path, state: dict[str, str]) -> None:
    path.write_text(json.dumps(state, indent=2))
    os.chmod(path, 0o600)
    logger.info("NVS (sim) 저장 → %s", path)


# ─────────────────────────────────────────────────────────────────────────
# 3. 디바이스 상태 (heater latch, msg_id 링버퍼)
# ─────────────────────────────────────────────────────────────────────────


class DeviceState:
    def __init__(self) -> None:
        self.relay = "OFF"
        self.fan = "OFF"
        self.heater_state = "OFF"
        self.heater_locked = False
        self.heater_on_since: float | None = None
        self.led_duty = 0  # 0~100
        self._msg_id_seen: deque[str] = deque(maxlen=MSG_ID_RINGBUFFER_SIZE)

    def is_msg_id_seen(self, msg_id: str) -> bool:
        if msg_id in self._msg_id_seen:
            return True
        self._msg_id_seen.append(msg_id)
        return False

    def telemetry_payload(self) -> dict[str, Any]:
        # 25±0.5°C, 60±2%RH 가짜 노이즈
        return {
            "ts": int(time.time()),
            "dht22_a": {
                "t": round(25.0 + random.uniform(-0.5, 0.5), 1),
                "h": round(60.0 + random.uniform(-2, 2), 1),
                "ok": True,
            },
            "dht22_b": {
                "t": round(24.5 + random.uniform(-0.5, 0.5), 1),
                "h": round(58.0 + random.uniform(-2, 2), 1),
                "ok": True,
            },
            "relay": self.relay,
            "fan": self.fan,
            "heater": {"state": self.heater_state, "locked": self.heater_locked},
        }

    def apply_command(self, action: str, payload: dict[str, Any]) -> str:
        """action 처리. result 문자열 반환 (ack 의 result 필드)."""
        if action == "relay_toggle":
            self.relay = "ON" if self.relay == "OFF" else "OFF"
            return "ok"
        if action == "fan_toggle":
            self.fan = "ON" if self.fan == "OFF" else "OFF"
            return "ok"
        if action == "heater_toggle":
            if self.heater_locked:
                return "rejected_locked"
            if self.heater_state == "OFF":
                self.heater_state = "ON"
                self.heater_on_since = time.time()
            else:
                self.heater_state = "OFF"
                self.heater_on_since = None
            return "ok"
        if action == "heater_clear":
            self.heater_locked = False
            return "ok"
        if action == "led_on":
            self.led_duty = 50
            return "ok"
        if action == "led_up":
            self.led_duty = min(100, self.led_duty + 10)
            return "ok"
        if action == "led_down":
            self.led_duty = max(0, self.led_duty - 10)
            return "ok"
        if action == "token_rotate":
            # 실제 NVS 갱신은 호출자가 처리 (state 파일 저장 + 재연결)
            return "ok"
        return "rejected_unknown_action"

    def check_heater_latch(self) -> dict[str, Any] | None:
        """heater 연속 가동 ≥ HEATER_LATCH_SEC 면 latch + alert payload 반환."""
        if self.heater_state == "ON" and self.heater_on_since is not None:
            elapsed = time.time() - self.heater_on_since
            if elapsed >= HEATER_LATCH_SEC and not self.heater_locked:
                self.heater_state = "OFF"
                self.heater_locked = True
                self.heater_on_since = None
                return {
                    "kind": "heater_latched",
                    "severity": "critical",
                    "message": f"히터 {int(elapsed)}초 연속 가동 — safety latch ON",
                    "context": {"duration_sec": int(elapsed)},
                }
        return None


# ─────────────────────────────────────────────────────────────────────────
# 4. MQTT 클라이언트
# ─────────────────────────────────────────────────────────────────────────


class SimDevice:
    def __init__(
        self,
        device_id: str,
        mqtt_token: str,
        host: str = DEFAULT_MQTT_HOST,
        port: int = DEFAULT_MQTT_PORT,
        state: DeviceState | None = None,
    ) -> None:
        self.device_id = device_id
        self.state = state or DeviceState()
        self._stop = threading.Event()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=device_id,
            clean_session=True,
        )
        self._client.username_pw_set(device_id, mqtt_token)
        self._client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._host = host
        self._port = port

    # ----- 라이프사이클 -----

    def start(self) -> None:
        logger.info("연결 시도: %s:%d (client_id=%s)", self._host, self._port, self.device_id)
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

        # telemetry 스레드 + heater latch 감시 스레드
        threading.Thread(target=self._telemetry_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("종료")

    def wait(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    # ----- paho 콜백 -----

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if reason_code != 0:
            logger.error("MQTT 연결 실패: %s", reason_code)
            return
        topic = f"esp32/{self.device_id}/command"
        client.subscribe(topic, qos=1)
        logger.info("MQTT 연결됨, subscribe %s", topic)

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props=None) -> None:
        logger.warning("MQTT 끊김: %s (자동 재연결 대기)", reason_code)

    def _on_message(self, _client, _userdata, msg: mqtt.MQTTMessage) -> None:
        if not msg.topic.endswith("/command"):
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("command JSON 파싱 실패: %s", msg.payload)
            return

        msg_id = payload.get("msg_id", "")
        issued_at = payload.get("issued_at", 0)
        ttl_sec = payload.get("ttl_sec", 10)
        action = payload.get("action", "")

        # 1) msg_id 중복 제거
        if msg_id and self.state.is_msg_id_seen(msg_id):
            self._publish_ack(msg_id, "rejected_duplicate_msg_id")
            return

        # 2) TTL 검증
        if issued_at and time.time() - issued_at > ttl_sec:
            self._publish_ack(msg_id, "rejected_ttl_expired")
            return

        # 3) 실행
        result = self.state.apply_command(action, payload)
        logger.info("command: %s → %s", action, result)
        self._publish_ack(msg_id, result)

    # ----- publish 헬퍼 -----

    def _publish_ack(self, msg_id: str, result: str) -> None:
        body = {
            "msg_id": msg_id,
            "result": result,
            "state": {
                "heater": self.state.heater_state,
                "locked": self.state.heater_locked,
            },
        }
        self._client.publish(
            f"esp32/{self.device_id}/ack",
            payload=json.dumps(body),
            qos=1,
            retain=False,
        )

    def _publish_telemetry(self) -> None:
        payload = self.state.telemetry_payload()
        self._client.publish(
            f"esp32/{self.device_id}/telemetry",
            payload=json.dumps(payload),
            qos=0,
            retain=False,
        )

    def _publish_alert(self, alert: dict[str, Any]) -> None:
        self._client.publish(
            f"esp32/{self.device_id}/alert",
            payload=json.dumps(alert),
            qos=1,
            retain=False,
        )

    def _telemetry_loop(self) -> None:
        while not self._stop.is_set():
            self._publish_telemetry()
            # heater latch 감시
            alert = self.state.check_heater_latch()
            if alert:
                logger.warning("ALERT: %s", alert["message"])
                self._publish_alert(alert)
            self._stop.wait(TELEMETRY_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────────────────
# 5. CLI
# ─────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="ESP32-S3 시뮬레이터 (펌웨어 참조 구현)")
    parser.add_argument("--pair", action="store_true", help="HTTPS POST /devices/pair 새로 발급")
    parser.add_argument("--jwt", help="Supabase access_token (--pair 시 필수)")
    parser.add_argument("--name", default="시뮬 디바이스", help="페어링 시 디바이스 이름")
    parser.add_argument("--species", default=None, help="페어링 시 종 (옵션)")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--mqtt-host", default=DEFAULT_MQTT_HOST)
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_MQTT_PORT)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="NVS 시뮬 경로")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    state_path = Path(args.state_file)
    creds = load_state(state_path)

    if args.pair or creds is None:
        if not args.jwt:
            raise SystemExit("--pair 또는 NVS 파일 없음 — --jwt 필수")
        creds = pair_device(args.api_base, args.jwt, args.name, args.species)
        save_state(state_path, creds)

    sim = SimDevice(
        device_id=creds["device_id"],
        mqtt_token=creds["mqtt_token"],
        host=args.mqtt_host,
        port=args.mqtt_port,
    )

    def _shutdown(_sig, _frame):
        sim.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sim.start()
    sim.wait()


if __name__ == "__main__":
    main()
