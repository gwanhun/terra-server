#!/usr/bin/env python
"""
카메라 1대에 MQTT 명령 1건을 발행하고 ack 를 기다린다 — 서버에 API 가 없는 운영 명령용.

    # SD 카드 전체 포맷(hbridge10+, 되돌릴 수 없음 — confirm 필수)
    uv run python scripts/camera_command.py --camera p4cam-a584f662 sd_format --json '{"confirm":"ERASE"}'

    # IR-cut 기준 조정(hbridge2+)
    uv run python scripts/camera_command.py --camera p4cam-a584f662 ircut_set --json '{"day_color_delta_x16":24}'

    # 재부팅(모든 0.4.x)
    uv run python scripts/camera_command.py --camera p4cam-a584f662 reboot

이 PC 의 .env 브로커 계정은 운영 브로커가 거부한다(2026-11-06) — **운영 서버 호스트에서** 실행한다.
펌웨어는 ack(`esp32/{camera_id}/ack`, 같은 msg_id) 를 먼저 보내고 결과 문자열을 `state` 에 담는다
(`accepted…`, `rejected_confirm…`, `rejected_unknown_action` 등). 실제 수행 결과는 camera_logs 로 올라온다.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from backend.mqtt.topics import topic_ack, topic_command  # noqa: E402
from backend.webrtc_signaling import MqttWebRTCSignaling  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", required=True, help="camera_id (예: p4cam-a584f662)")
    p.add_argument("action", help="펌웨어 action 이름 (sd_format, ircut_set, reboot …)")
    p.add_argument("--json", default="{}", help="추가 필드 JSON 객체")
    p.add_argument("--ttl", type=int, default=60, help="ttl_sec (기본 60)")
    p.add_argument("--wait", type=float, default=10.0, help="ack 대기 초 (기본 10)")
    a = p.parse_args()

    extra = json.loads(a.json)
    if not isinstance(extra, dict):
        print("--json 은 객체여야 함", file=sys.stderr)
        return 2
    cmd = {"msg_id": str(uuid4()), "issued_at": int(time.time()), "ttl_sec": a.ttl, "action": a.action, **extra}

    sig = MqttWebRTCSignaling()
    acks: list[dict] = []
    got = threading.Event()
    client = sig._new_client()  # noqa: SLF001 — 같은 브로커 계정으로 ack 구독

    def on_msg(_c, _u, m):  # noqa: ANN001
        try:
            d = json.loads(m.payload.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return
        if d.get("msg_id") == cmd["msg_id"]:
            acks.append(d)
            got.set()

    client.on_message = on_msg
    client.connect(sig._broker_host, sig._broker_port, keepalive=30)  # noqa: SLF001
    client.subscribe(topic_ack(a.camera), qos=1)
    client.loop_start()
    try:
        sig.publish(a.camera, cmd)
        print(f"발행: {topic_command(a.camera)} {json.dumps(cmd, ensure_ascii=False)}")
        if not got.wait(a.wait):
            print(f"ack 없음({a.wait:.0f}s) — 카메라 오프라인이거나 구 펌웨어(action 미지원 시에도 ack 는 옴)")
            return 1
        for d in acks:
            print("ack:", json.dumps(d, ensure_ascii=False))
        return 0
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
