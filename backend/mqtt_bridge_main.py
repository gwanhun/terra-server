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
| `OfflineMonitor`    | devices.last_seen_at 감시 → offline alert (1분 주기) |

모두 같은 프로세스 안. 셋 다 SIGTERM 에서 graceful shutdown.
"""

from __future__ import annotations

import logging
import signal
import sys

from backend.mqtt.bridge import MqttBridge
from backend.mqtt.dispatcher import CommandDispatcher
from backend.offline_monitor import OfflineMonitor


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run() -> None:
    _setup_logging()
    bridge = MqttBridge()
    dispatcher = CommandDispatcher(bridge)
    offline_monitor = OfflineMonitor()

    def _shutdown(_signum: int, _frame) -> None:
        offline_monitor.stop()
        dispatcher.stop()
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    bridge.start()
    dispatcher.start()
    offline_monitor.start()
    bridge.wait_stopped()


if __name__ == "__main__":
    run()
