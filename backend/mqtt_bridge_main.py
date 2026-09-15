"""MQTT 브리지 진입점.

실행:
    uv run terra-bridge

systemd 에서는:
    ExecStart=/home/ubuntu/terra-server/.venv/bin/terra-bridge

## 가동 컴포넌트

| 컴포넌트 | 역할 |
|----------|------|
| `MqttBridge`        | Mosquitto ↔ Supabase (telemetry/ack/alert 수신) |
| `CommandDispatcher` | Supabase commands(pending) → MQTT publish (1초 polling) |
| `ScheduleRunner`    | Supabase schedules(due) → commands INSERT (30초 polling) |
| `OfflineMonitor`    | devices.last_seen_at 감시 → offline alert (1분 주기) |
| `PushOutboxWorker`  | push_outbox(pending) → 앱 Edge Function POST (5초 polling) |

모두 같은 프로세스 안. 전부 SIGTERM 에서 graceful shutdown.
PushOutboxWorker 는 PUSH_EVENT_INGEST_URL/SECRET 미설정이면 시작하지 않는다.
"""

from __future__ import annotations

import logging
import signal
import sys

from backend.mqtt.bridge import MqttBridge
from backend.mqtt.dispatcher import CommandDispatcher
from backend.offline_monitor import OfflineMonitor
from backend.push_events import PushOutboxWorker
from backend.schedule_runner import ScheduleRunner


# 서드파티 로거 소음 억제.
# supabase-py 는 모든 REST 호출을 httpx INFO 로 찍는다. 디바이스 4대(3초 주기)만으로도
# 하루 30만 줄이 넘어가 정작 우리 WARNING 이 묻힌다 (2026-09-15 운영 로그 확인).
# 우리 코드의 로그는 그대로 두고 HTTP 클라이언트만 WARNING 으로 올린다.
_NOISY_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3")


def _quiet_third_party_loggers() -> None:
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    _quiet_third_party_loggers()


def run() -> None:
    _setup_logging()
    bridge = MqttBridge()
    dispatcher = CommandDispatcher(bridge)
    schedule_runner = ScheduleRunner()
    offline_monitor = OfflineMonitor()
    push_worker = PushOutboxWorker()

    def _shutdown(_signum: int, _frame) -> None:
        push_worker.stop()
        offline_monitor.stop()
        schedule_runner.stop()
        dispatcher.stop()
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    bridge.start()
    dispatcher.start()
    schedule_runner.start()
    offline_monitor.start()
    push_worker.start()
    bridge.wait_stopped()


if __name__ == "__main__":
    run()
