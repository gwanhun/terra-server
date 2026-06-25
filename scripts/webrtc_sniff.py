"""WebRTC 시그널링 진단용 MQTT 스니퍼 (펌웨어 무수정).

esp32/+/command 와 esp32/+/ack 토픽을 동시에 구독해서, 웹에서 라이브를 누를 때
- offer 명령의 payload 크기 (8192 초과 = 단편화 위험)
- 카메라가 ack 토픽에 webrtc_answer 를 발행하는지 / 아무것도 안 오는지
를 눈으로 확인한다.

접속 방식은 backend/webrtc_signaling.py 와 동일 (.env 의 브리지 계정 + TLS).
실행: uv run python scripts/webrtc_sniff.py
중단: Ctrl-C
"""

from __future__ import annotations

import json
import os
import ssl
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
PORT = int(os.getenv("MQTT_BROKER_PORT", "8883"))
USER = os.getenv("MQTT_BRIDGE_USERNAME", "")
PASS = os.getenv("MQTT_BRIDGE_PASSWORD", "")
CA = os.getenv("MQTT_CA_CERT_PATH", "").strip() or None
USE_TLS = os.getenv("MQTT_USE_TLS", "true").lower() == "true"


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def on_connect(c, _u, _f, rc, _p=None):
    if rc != 0:
        print(f"[{_stamp()}] ❌ 브로커 접속 실패: rc={rc}")
        return
    print(f"[{_stamp()}] ✅ 접속됨 ({HOST}:{PORT}) — command/ack 구독, 이제 웹에서 라이브 눌러봐\n")
    c.subscribe("esp32/+/command", qos=1)
    c.subscribe("esp32/+/ack", qos=1)


def on_message(_c, _u, msg: mqtt.MQTTMessage):
    raw = msg.payload
    size = len(raw)
    kind = msg.topic.split("/")[-1]  # command | ack
    flag = "  ⚠️ 8192 초과 → 단편화!" if size > 8192 else ""

    action = "?"
    try:
        action = (json.loads(raw.decode("utf-8")).get("action")
                  or json.loads(raw.decode("utf-8")).get("type") or "?")
    except Exception:
        pass

    print(f"[{_stamp()}] {msg.topic}")
    print(f"          kind={kind} action={action} size={size}B{flag}")

    # offer/answer 는 전문을 보고 싶을 때가 있어 앞부분만 출력
    if action in ("webrtc_offer", "webrtc_answer") or size > 1000:
        preview = raw.decode("utf-8", "replace")
        print(f"          payload[:400]= {preview[:400]}")
    print()


def main() -> None:
    if not USER or not PASS:
        raise SystemExit("MQTT_BRIDGE_USERNAME / MQTT_BRIDGE_PASSWORD 가 .env 에 없음")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="terra-webrtc-sniff", clean_session=True)
    client.username_pw_set(USER, PASS)
    if USE_TLS:
        client.tls_set(ca_certs=CA, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[{_stamp()}] 접속 시도: {HOST}:{PORT} user={USER} tls={USE_TLS}")
    client.connect(HOST, PORT, keepalive=30)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
