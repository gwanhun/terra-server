"""MQTT 브리지 진입점.

실행:
    uv run terra-bridge

systemd 에서는:
    ExecStart=/home/ubuntu/terra-server/.venv/bin/terra-bridge
"""

from __future__ import annotations

import logging
import signal
import sys

from backend.mqtt.bridge import MqttBridge


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run() -> None:
    _setup_logging()
    bridge = MqttBridge()

    def _shutdown(_signum: int, _frame) -> None:
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    bridge.start()
    bridge.wait_stopped()


if __name__ == "__main__":
    run()
