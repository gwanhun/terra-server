"""
명령 디스패처 — Stage C.

흐름:
    앱(웹) → Supabase commands INSERT (status='pending')
       └─→ 본 디스패처가 1초 polling → MQTT publish → status='sent'
           └─→ 디바이스 실행 → ack
               └─→ handlers.handle_ack 가 status='acked' UPDATE

## Polling vs Realtime
스펙 ([specs/stage-c-command-dispatch.md](../../specs/stage-c-command-dispatch.md)) 의 1순위 권장은 Realtime
이지만 supabase-py 의 Realtime 은 asyncio 인데 bridge.py 는 paho + threading (sync).
두 이벤트 루프 통합 복잡 → polling 으로 시작. 부하 평가 후 Realtime 으로 마이그 가능.

## TTL 만료 처리
서버 측에서도 검증 — pending 인데 issued_at + ttl_sec 가 지났으면 publish 안 하고 expired 처리.
펌웨어 TTL 검증의 보완책 (publish 안 됨 → 디바이스가 stale 명령 받지도 않음).

## 동시 처리
- 한 poll 에서 batch (기본 50개) 까지 처리
- publish 성공 → status='sent' UPDATE (이 순서가 중요: UPDATE 먼저 하면 publish 실패 시 좀비 상태)
- publish 실패 → 그대로 pending 유지, 다음 poll 에서 재시도

## 멀티 인스턴스 주의
- 본 디스패처는 **단일 프로세스 가정**. terra-bridge.service 가 1개 인스턴스.
- 멀티 인스턴스 띄우면 같은 command 를 여러 번 publish 할 수 있음 (race).
- 그땐 SELECT ... FOR UPDATE SKIP LOCKED 또는 advisory lock 필요.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.mqtt import handlers
from backend.supabase_client import get_supabase_client

if TYPE_CHECKING:
    from backend.mqtt.bridge import MqttBridge

logger = logging.getLogger(__name__)


DEFAULT_INTERVAL_SEC = 1.0
DEFAULT_BATCH = 50
DEFAULT_TTL_SEC = 10  # commands.ttl_sec 가 NULL/0 일 때 fallback


def _parse_iso(ts: str) -> datetime:
    """Supabase 가 반환하는 timestamp string → datetime (UTC).

    'YYYY-MM-DDTHH:MM:SS.uuuuuu+00:00' 또는 '...Z' 형식 모두 지원.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def poll_and_dispatch(bridge: "MqttBridge", batch: int = DEFAULT_BATCH) -> int:
    """1회 polling — pending commands 처리. 처리한 row 수 반환."""
    sb = get_supabase_client()

    res = (
        sb.table("commands")
        .select("id, device_id, action, payload, issued_at, ttl_sec")
        .eq("status", "pending")
        .order("issued_at")
        .limit(batch)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return 0

    processed = 0
    for row in rows:
        try:
            _dispatch_one(bridge, row)
            processed += 1
        except Exception:  # noqa: BLE001
            logger.exception("dispatch 실패 (command_id=%s)", row.get("id"))
    return processed


def _dispatch_one(bridge: "MqttBridge", row: dict[str, Any]) -> None:
    cmd_id = row["id"]
    device_uuid = row["device_id"]
    action = row["action"]
    ttl = row.get("ttl_sec") or DEFAULT_TTL_SEC

    sb = get_supabase_client()

    # 1) TTL 만료 검증
    issued_at = _parse_iso(row["issued_at"])
    age = (datetime.now(timezone.utc) - issued_at).total_seconds()
    if age > ttl:
        sb.table("commands").update({"status": "expired"}).eq("id", cmd_id).execute()
        logger.info("command %s expired (age=%.1fs, ttl=%ds)", cmd_id, age, ttl)
        return

    # 2) device UUID → device_id (TEXT) 캐시 해상
    device_text = handlers._cached_device_text(device_uuid)
    if not device_text:
        sb.table("commands").update(
            {"status": "rejected", "result": "unknown_device"}
        ).eq("id", cmd_id).execute()
        logger.warning("command %s: unknown device_uuid=%s", cmd_id, device_uuid)
        return

    # 3) payload 구성 ([docs/MQTT.md](../../docs/MQTT.md) §2)
    publish_payload: dict[str, Any] = {
        "msg_id": cmd_id,
        "issued_at": int(issued_at.timestamp()),
        "ttl_sec": ttl,
        "action": action,
    }
    extra = row.get("payload") or {}
    if isinstance(extra, dict):
        publish_payload.update(extra)

    # 4) MQTT publish
    success = bridge.publish_command(device_text, publish_payload)
    if not success:
        # 좀비 방지: 그대로 pending 유지, 다음 poll 재시도
        logger.warning("command %s publish 실패 — 다음 poll 재시도", cmd_id)
        return

    # 5) status='sent' UPDATE
    sb.table("commands").update({"status": "sent"}).eq("id", cmd_id).execute()
    logger.info("command %s → %s (%s)", cmd_id, device_text, action)


class CommandDispatcher:
    """별도 스레드에서 1초 polling. start()/stop() 으로 라이프사이클 제어."""

    def __init__(self, bridge: "MqttBridge", interval_sec: float = DEFAULT_INTERVAL_SEC):
        self._bridge = bridge
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="command-dispatcher"
        )
        self._thread.start()
        logger.info("command dispatcher 시작 (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("command dispatcher 정지")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                n = poll_and_dispatch(self._bridge)
                if n > 0:
                    logger.debug("dispatched %d commands", n)
            except Exception:  # noqa: BLE001
                logger.exception("dispatcher poll 실패")
            self._stop.wait(self._interval)


__all__ = [
    "CommandDispatcher",
    "DEFAULT_BATCH",
    "DEFAULT_INTERVAL_SEC",
    "DEFAULT_TTL_SEC",
    "poll_and_dispatch",
]
