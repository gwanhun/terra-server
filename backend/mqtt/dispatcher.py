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
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from backend.command_service import MIST_ACTION, insert_pending_command
from backend.mqtt import handlers
from backend.push_events import NO_ACK_RESULT
from backend.supabase_client import get_supabase_client

if TYPE_CHECKING:
    from backend.mqtt.bridge import MqttBridge

logger = logging.getLogger(__name__)


# ACK 가 이 시간 안에 안 오면 실패로 굳힌다 (앱 회신 2026-09-16 §3-6, 30초).
# 일반 명령 TTL(10초)보다 넉넉히 잡아 펌웨어 재시도·네트워크 지연을 흡수한다.
NO_ACK_THRESHOLD_SEC = 30.0
# 무응답 스윕 주기 — 1초 폴링마다 돌릴 필요는 없다.
NO_ACK_SWEEP_INTERVAL_SEC = 10.0

# 펌웨어가 구현하지 않은 action — 발행해도 unknown_action 만 돌아온다. 발행 전에 거절해
# 앱이 즉시 결과를 보게 한다 (앱 요청 2026-09-16 §3). 히터 보드가 생기면 capabilities
# 플래그로 다시 연다. schedules 화이트리스트에서도 제외돼 있다.
UNSUPPORTED_ACTIONS: frozenset[str] = frozenset({
    "heater_on", "heater_off", "heater_toggle", "heater_clear_lock",
})

# MQTT 명령 프로토콜이 쓰는 필드 — payload 로 덮어쓸 수 없다 (docs/MQTT.md §2).
_RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "msg_id", "issued_at", "ttl_sec", "action",
})

DEFAULT_INTERVAL_SEC = 1.0
DEFAULT_BATCH = 50
DEFAULT_TTL_SEC = 10  # commands.ttl_sec 가 NULL/0 일 때 fallback

# mist 분할 발행(2026-09-23): 첫 분사 종료 → 후속 발행까지 여유. 펌웨어 재기동 가드 200ms +
# 폴링 1초 + MQTT 왕복을 덮는다. 너무 짧으면 후속이 `busy` 로 버려져 물이 덜 나간다.
MIST_SPLIT_GAP_SEC = 1.5


def _parse_iso(ts: str) -> datetime:
    """Supabase 가 반환하는 timestamp string → datetime (UTC).

    'YYYY-MM-DDTHH:MM:SS.uuuuuu+00:00' 또는 '...Z' 형식 모두 지원.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _enqueue_failure(row: dict[str, Any], device_uuid: str, result: str) -> None:
    """ACK 없이 끝난 명령을 푸시 실패 이벤트로 적재. 절대 예외를 올리지 않는다.

    지연 import: push_events 가 dispatcher 를 쓰지는 않지만 handlers 쪽과 관례를 맞춘다.
    """
    try:
        from backend import push_events

        device_key = handlers._cached_device_text(device_uuid) or ""
        push_events.enqueue_command_failure(
            row, device_key, handlers.device_meta(device_uuid), result
        )
    except Exception:  # noqa: BLE001
        logger.exception("실패 푸시 이벤트 적재 실패 (command_id=%s)", row.get("id"))


def sweep_unacked(threshold_sec: float = NO_ACK_THRESHOLD_SEC, batch: int = DEFAULT_BATCH) -> int:
    """발행했는데 ACK 가 안 온 명령을 실패로 굳힌다. 처리 건수 반환.

    기존에는 `sent` 인 채로 영원히 방치돼서 앱이 결과를 알 수 없었다
    (2026-09-15 회신 §4.4 에서 제기 → 앱이 "필요하다" 회신, 2026-09-16 §3-6).
    무인 실행(예약)에서 히터·펌프가 안 켜진 경우가 사용자에게 가장 중요한 알림이다.
    """
    sb = get_supabase_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=threshold_sec)).isoformat()
    try:
        res = (
            sb.table("commands")
            .select("id, device_id, action, payload, issued_at, ttl_sec, issued_by, source, source_id")
            .eq("status", "sent")
            .lt("issued_at", cutoff)
            .order("issued_at")
            .limit(batch)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("무응답 명령 조회 실패")
        return 0

    rows = res.data or []
    swept = 0
    for row in rows:
        cmd_id = row["id"]
        try:
            upd = (
                sb.table("commands")
                .update({"status": "no_ack", "result": NO_ACK_RESULT})
                .eq("id", cmd_id)
                .eq("status", "sent")          # 그 사이 ACK 가 왔으면 건드리지 않는다
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("무응답 처리 실패 (command_id=%s)", cmd_id)
            continue
        if not upd.data:
            continue                            # 경합 — ACK 가 먼저 들어옴
        logger.warning("command %s 무응답(%ds 경과) → no_ack", cmd_id, int(threshold_sec))
        _enqueue_failure(row, row["device_id"], NO_ACK_RESULT)
        swept += 1
    return swept


def poll_and_dispatch(bridge: "MqttBridge", batch: int = DEFAULT_BATCH) -> int:
    """1회 polling — pending commands 처리. 처리한 row 수 반환."""
    sb = get_supabase_client()

    res = (
        sb.table("commands")
        # issued_by/source/source_id 는 실패 푸시 이벤트 구성에 쓴다 (앱 회신 2026-09-16 §3-6)
        .select("id, device_id, action, payload, issued_at, ttl_sec, issued_by, source, source_id")
        .eq("status", "pending")
        .order("issued_at")
        .limit(batch)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return 0

    processed = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        # 예약 발행: issued_at 이 미래면 아직 때가 아니다 — pending 그대로 둔다 (mist 분할 후속 분사).
        try:
            if _parse_iso(row["issued_at"]) > now:
                continue
        except (KeyError, ValueError, TypeError):
            pass
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
        sb.table("commands").update(
            {"status": "expired", "result": "expired"}
        ).eq("id", cmd_id).execute()
        logger.info("command %s expired (age=%.1fs, ttl=%ds)", cmd_id, age, ttl)
        _enqueue_failure(row, device_uuid, "expired")
        return

    # 1.5) 펌웨어 미구현 action — 발행 전 거절
    if action in UNSUPPORTED_ACTIONS:
        sb.table("commands").update(
            {"status": "rejected", "result": "unsupported_action"}
        ).eq("id", cmd_id).execute()
        logger.warning("command %s: 미지원 action %s → rejected", cmd_id, action)
        _enqueue_failure(row, device_uuid, "unsupported_action")
        return

    # 2) device UUID → device_id (TEXT) 캐시 해상
    device_text = handlers._cached_device_text(device_uuid)
    if not device_text:
        sb.table("commands").update(
            {"status": "rejected", "result": "unknown_device"}
        ).eq("id", cmd_id).execute()
        logger.warning("command %s: unknown device_uuid=%s", cmd_id, device_uuid)
        _enqueue_failure(row, device_uuid, "unknown_device")
        return

    # 2.5) mist 분할 (2026-09-23): 요청이 기기 상한(capabilities.mist_max_ms, 미보고=5000)을
    #      넘으면 상한만큼 먼저 분사하고 나머지는 후속 명령으로 예약 발행한다. 펌웨어는 상한
    #      초과를 조용히 clamp 하므로(10초 요청 → 5초 분사) 그대로 보내면 안 되고, 펌웨어 플래시
    #      없이 10초를 채우는 방법은 이것뿐이다. 분사마다 펌웨어 타이머가 끄므로 후속이 유실돼도
    #      "물이 덜 나옴"일 뿐 펌프가 켜진 채 남지 않는다. 신 펌웨어(상한 ≥ 요청)는 한 번에 간다.
    burst_ms: int | None = None
    remainder_ms = 0
    if action == MIST_ACTION:
        extra0 = row.get("payload")
        dur = extra0.get("duration_ms") if isinstance(extra0, dict) else None
        if isinstance(dur, (int, float)) and not isinstance(dur, bool):
            cap = handlers.device_mist_max_ms(device_uuid)
            if dur > cap:
                burst_ms, remainder_ms = cap, int(dur) - cap

    # 3) payload 구성 ([docs/MQTT.md](../../docs/MQTT.md) §2)
    publish_payload: dict[str, Any] = {
        "msg_id": cmd_id,
        "issued_at": int(issued_at.timestamp()),
        "ttl_sec": ttl,
        "action": action,
    }
    extra = row.get("payload") or {}
    if isinstance(extra, dict):
        # commands.payload 는 앱이 직접 INSERT 하는 값이라(RLS 허용) 예약어를 덮어쓸 수
        # 있었다. 예: led_on 예약의 payload 에 {"action": "heater_on"} 이 있으면 엉뚱한
        # 명령이 나간다. 프로토콜 필드는 서버가 정한 값만 쓰고, 나머지 인자
        # (brightness / duration_ms / new_token …)만 병합한다.
        safe = {k: v for k, v in extra.items() if k not in _RESERVED_PAYLOAD_KEYS}
        dropped = set(extra) - set(safe)
        if dropped:
            logger.warning(
                "command %s payload 의 예약 키 무시: %s", cmd_id, sorted(dropped)
            )
        publish_payload.update(safe)
    if burst_ms is not None:
        publish_payload["duration_ms"] = burst_ms   # DB 행은 요청값(10000) 그대로, 기기엔 상한만큼

    # 4) MQTT publish
    success = bridge.publish_command(device_text, publish_payload)
    if not success:
        # 좀비 방지: 그대로 pending 유지, 다음 poll 재시도
        logger.warning("command %s publish 실패 — 다음 poll 재시도", cmd_id)
        return

    # 5) status='sent' UPDATE — pending 일 때만. 디바이스가 publish 직후(수백 ms) ack 하면
    #    handle_ack 의 'acked' 가 먼저 들어올 수 있는데, 무조건 덮어쓰면 'sent' 로 되돌아가
    #    sweep_unacked 가 no_ack 로 오판한다 (acked_at 은 찍혀 있는데 status=no_ack 인 행).
    sb.table("commands").update({"status": "sent"}).eq("id", cmd_id).eq(
        "status", "pending"
    ).execute()
    logger.info("command %s → %s (%s)", cmd_id, device_text, action)

    # 6) mist 분할 나머지 — 첫 분사가 **실제로 발행된 뒤**에만 예약한다 (publish 실패 시 후속만
    #    홀로 나가는 일 방지). 발행 시각 = 지금 + 첫 분사 길이 + 여유.
    if burst_ms is not None and remainder_ms > 0:
        _enqueue_mist_remainder(sb, row, burst_ms, remainder_ms)


def _enqueue_mist_remainder(sb: Any, row: dict[str, Any], burst_ms: int, remainder_ms: int) -> None:
    """분할된 mist 의 나머지를 후속 명령으로 예약 발행(issued_at = 첫 분사 종료 + MIST_SPLIT_GAP_SEC).

    source='timer' — 서버가 시간 기준으로 만든 명령. push_events 는 schedule 만 보내므로 후속 분사가
    "분무 시작" 푸시를 중복으로 내지 않는다. reason 에 원 명령 id 를 남겨 감사 로그에서 묶인다.
    나머지가 아직 상한을 넘으면(예: 상한 3000 에 10000) 후속이 발행될 때 다시 분할된다 — 재귀 불필요.
    """
    due = (datetime.now(timezone.utc)
           + timedelta(milliseconds=burst_ms) + timedelta(seconds=MIST_SPLIT_GAP_SEC))
    try:
        inserted = insert_pending_command(
            sb,
            device_uuid=row["device_id"],
            action=MIST_ACTION,
            payload={"duration_ms": remainder_ms},
            issued_by=row.get("issued_by"),
            ttl_sec=DEFAULT_TTL_SEC,
            source="timer",
            reason=f"mist split of {row['id']} (+{remainder_ms}ms)",
            issued_at=due,
        )
    except Exception:  # noqa: BLE001
        logger.exception("mist 후속 분사 INSERT 실패 (parent=%s)", row.get("id"))
        return
    if inserted is None:
        logger.error("mist 후속 분사 INSERT 실패 (parent=%s)", row.get("id"))
        return
    logger.info("command %s mist 분할: %dms 발행, 나머지 %dms → %s (%s)",
                row["id"], burst_ms, remainder_ms, inserted.get("id"), due.isoformat())


class CommandDispatcher:
    """별도 스레드에서 1초 polling. start()/stop() 으로 라이프사이클 제어."""

    def __init__(self, bridge: "MqttBridge", interval_sec: float = DEFAULT_INTERVAL_SEC):
        self._bridge = bridge
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_sweep = 0.0

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

            # 무응답 스윕은 1초마다 돌 필요가 없어 별도 주기로 (같은 스레드 = 경합 없음)
            now = time.monotonic()
            if now >= self._next_sweep:
                self._next_sweep = now + NO_ACK_SWEEP_INTERVAL_SEC
                try:
                    swept = sweep_unacked()
                    if swept:
                        logger.info("무응답 명령 %d건 정리", swept)
                except Exception:  # noqa: BLE001
                    logger.exception("무응답 스윕 실패")

            self._stop.wait(self._interval)


__all__ = [
    "CommandDispatcher",
    "DEFAULT_BATCH",
    "DEFAULT_INTERVAL_SEC",
    "DEFAULT_TTL_SEC",
    "NO_ACK_THRESHOLD_SEC",
    "UNSUPPORTED_ACTIONS",
    "poll_and_dispatch",
    "sweep_unacked",
]
