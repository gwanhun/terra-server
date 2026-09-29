"""
예약 on/off 명령 재전달 (2026-09-29, 설계 specs/camera-reliability-2026-09.md P2-(a)).

## 문제
예약 명령은 TTL 10초·재시도 없음이다. 기기가 잠깐 끊겨 있거나(expired), 발행 뒤 ACK 가 안 오거나
(no_ack), 기기 조회가 일시 실패하면(unknown_device — DB 예외도 여기로 떨어진다) 그 예약은 그대로
버려진다. 09-29 현장에서 냉각팬 fan2_off 가 거부돼 1시간 넘게 돌았다.

## 동작
- 실패를 메모리에 기억한다(기기·액추에이터별 최신 1건). 대상: source='schedule' 이고 조명·팬·냉각팬의
  on/off 절대 상태 명령만(schedule_restore.ON_OFF_ACTIONS).
  제외: mist(이중 분무 > 누락), relay(펌프 — owner 결정 09-29), *_toggle(절대 상태 아님),
  duration_ms one-shot(늦게 켜면 끝 시각이 밀림), 수동 명령(사용자가 이미 실패를 봤다).
- 그 기기의 telemetry 가 들어오면(= 기기가 살아 있음) 원래 발행 시각 + REDELIVER_GRACE_SEC 안일 때
  1회 재큐잉. telemetry 가 이미 목표 상태면 조용히 버린다(ACK 만 유실된 경우).
- 실패 뒤 같은 액추에이터에 다른 명령이 발행되면(수동 포함) 그게 최신 의도라 취소한다.
- 재큐잉은 source='restore' — 예약 푸시(source='schedule' 만)가 또 나가지 않고, restore 는 다시
  재전달 대상이 아니라 1회로 끝난다. reason 에 원 명령 id 를 남긴다.

메모리라 브리지 재시작 시 잃는다 — 유예가 5분이라 감수(단일 브리지 프로세스 가정, dispatcher 와 동일).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from backend.command_service import insert_pending_command
from backend.schedule_restore import ON_OFF_ACTIONS, RESTORE_SOURCE

logger = logging.getLogger(__name__)

REDELIVER_GRACE_SEC = 300          # 원래 발행 시각 기준 5분 (설계 §1 P2-(a))
REDELIVER_SOURCES = frozenset({"schedule"})

# action → (actuator, 목표 telemetry 값)
_TARGETS: dict[str, tuple[str, str]] = {
    **{on: (act, "ON") for act, (on, _off) in ON_OFF_ACTIONS.items()},
    **{off: (act, "OFF") for act, (_on, off) in ON_OFF_ACTIONS.items()},
}

_pending: dict[str, dict[str, dict[str, Any]]] = {}   # device_uuid → actuator → 실패 명령 정보
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _pending.clear()


def pending_for(device_uuid: str) -> dict[str, dict[str, Any]]:
    """디버그·테스트용 스냅샷."""
    with _lock:
        return dict(_pending.get(device_uuid, {}))


def note_failure(row: dict[str, Any], result: str, now: float | None = None) -> None:
    """dispatcher 가 명령을 실패로 굳힐 때 호출. 대상이 아니면 무시. 예외를 올리지 않는다."""
    now = time.time() if now is None else now
    if row.get("source") not in REDELIVER_SOURCES:
        return
    target = _TARGETS.get(row.get("action") or "")
    if target is None:
        return
    payload = row.get("payload")
    if isinstance(payload, dict) and payload.get("duration_ms"):
        return
    try:
        issued = datetime.fromisoformat(str(row["issued_at"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return
    deadline = issued + REDELIVER_GRACE_SEC
    if now > deadline:
        return
    actuator, desired = target
    with _lock:
        _pending.setdefault(row["device_id"], {})[actuator] = {
            "id": row.get("id"), "action": row["action"], "desired": desired,
            "deadline": deadline, "result": result,
            "issued_by": row.get("issued_by"), "source_id": row.get("source_id"),
        }
    logger.info("command %s(%s) %s — 기기 복귀 시 재전달 대기 (%ds 이내)",
                row.get("id"), row["action"], result, int(deadline - now))


def note_dispatch(device_uuid: str, action: str) -> None:
    """같은 액추에이터에 새 명령이 발행 시도되면 대기 중인 재전달을 취소한다."""
    target = _TARGETS.get(action)
    if target is None:
        return
    with _lock:
        per_dev = _pending.get(device_uuid)
        if per_dev and per_dev.pop(target[0], None) is not None:
            logger.info("device %s: %s 새 명령(%s) → 재전달 취소", device_uuid, target[0], action)
            if not per_dev:
                _pending.pop(device_uuid, None)


def on_telemetry(sb: Any, device_uuid: str, state: dict[str, Any], now: float | None = None) -> None:
    """기기 telemetry 수신 시 호출. 대기 중인 재전달이 있으면 1회 큐잉. 예외를 올리지 않는다."""
    with _lock:
        per_dev = _pending.pop(device_uuid, None)
    if not per_dev:
        return
    now = time.time() if now is None else now
    for actuator, item in per_dev.items():
        if now > item["deadline"]:
            logger.warning("command %s(%s) 재전달 포기 — 유예 %ds 초과",
                           item["id"], item["action"], REDELIVER_GRACE_SEC)
            continue
        if state.get(actuator) == item["desired"]:
            logger.info("command %s(%s) 재전달 생략 — 이미 %s=%s",
                        item["id"], item["action"], actuator, item["desired"])
            continue
        try:
            inserted = insert_pending_command(
                sb,
                device_uuid=device_uuid,
                action=item["action"],
                issued_by=item["issued_by"],
                source=RESTORE_SOURCE,
                source_id=item["source_id"],
                reason=f"redeliver of {item['id']} ({item['result']})",
            )
        except Exception:  # noqa: BLE001 — 재전달은 보조 경로. telemetry 처리를 막지 않는다
            logger.exception("command %s 재전달 INSERT 실패", item["id"])
            continue
        logger.warning("command %s(%s) 재전달 → %s", item["id"], item["action"],
                       (inserted or {}).get("id"))


__all__ = [
    "REDELIVER_GRACE_SEC",
    "note_dispatch",
    "note_failure",
    "on_telemetry",
    "pending_for",
    "reset",
]
