"""앱 푸시 이벤트 — outbox 적재 + 전송 워커.

계약: docs/BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md (앱 요청 원문 2026-09-15).

## 왜 outbox 인가

명령 ACK 는 MQTT 수신 스레드에서 확정된다(`backend/mqtt/handlers.handle_ack`).
거기서 바로 HTTP 를 때리면 수신 루프가 외부 지연에 묶이고, 5xx 재시도도 못 한다.
→ ACK 확정 직후 `push_outbox` 에 INSERT 만 하고(빠름), 별도 워커가 전송한다.
브리지가 죽어도 미전송 이벤트는 DB 에 남는다.

## 발행 범위 (2026-09-15 현재)

앱 요청이 "사용자가 버튼을 누른 즉시 제어는 제외" 라서 `commands.source` 가
schedule/guard 인 것만 보낸다. `PUSH_EVENT_SOURCES` 로 조정 가능.

⚠️ `device.action.ended` 는 아직 발행하지 않는다. 지속시간 명령의 자동 OFF 는 펌웨어
one-shot 타이머라 종료 ACK 가 오지 않는다(회신 §3). 구간 예약의 off 명령 ACK 는
그 자체가 별도 command 라 `started` 로 나가며, 앱 답변이 오면 phase 매핑만 바꾸면 된다.

## 보안

ingest secret 은 `.env` 의 `PUSH_EVENT_INGEST_SECRET` 에만 둔다. outbox body 나
로그에 절대 넣지 않는다 — 헤더로만 실린다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

EVENT_STARTED = "device.action.started"
EVENT_ENDED = "device.action.ended"
EVENT_FAILED = "device.action.failed"

# 펌웨어가 성공을 알리는 유일한 값. 나머지(busy/error/unknown_action/rejected_* …)는
# 전부 실패로 묶는다 — status 는 결과와 무관하게 항상 'acked' 라 못 쓴다(회신 §4).
RESULT_OK = "ok"

# 앱 회신 2026-09-16 §3-2·§3-5: 발행 조건은 source='schedule' 만.
#   - manual(즉시 제어): 앱이 15초 ACK 응답으로 이미 처리 → 제외
#   - guard(가드 스킵): 1차 제외. 2차에 device.action.skipped 타입으로 별도 설계
#   - timer: 서버에서 세팅된 적이 없는 값 → 앱이 허용값에서 제거하기로 함
DEFAULT_SOURCES = ("schedule",)

# ACK 가 영영 오지 않는 명령을 실패로 굳히는 기준 (앱 회신 §3-6).
# 무인 실행에서 히터·펌프가 안 켜진 경우가 사용자에게 가장 중요한 알림이다.
NO_ACK_RESULT = "no_ack"

DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_BATCH = 20
DEFAULT_TIMEOUT_SEC = 10.0
MAX_ATTEMPTS = 8
# 지수 백오프 상한 — 5분. 8회면 대략 30분 이상 재시도.
MAX_BACKOFF_SEC = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 설정 ----------


def _enabled() -> bool:
    """URL 과 secret 이 모두 있어야 켜진다. 미설정이면 조용히 no-op."""
    return bool(_ingest_url() and _ingest_secret())


def _ingest_url() -> str:
    return (os.getenv("PUSH_EVENT_INGEST_URL") or "").strip()


def _ingest_secret() -> str:
    return (os.getenv("PUSH_EVENT_INGEST_SECRET") or "").strip()


def _sources() -> tuple[str, ...]:
    raw = (os.getenv("PUSH_EVENT_SOURCES") or "").strip()
    if not raw:
        return DEFAULT_SOURCES
    return tuple(s.strip() for s in raw.split(",") if s.strip())


# ---------- 적재 (ACK 스레드에서 호출) ----------


# ---------- 구간 예약 판별 ----------
#
# 앱 회신 2026-09-16 §3-1: `ended` 는 **구간 예약(pair_id 로 묶인 on/off 쌍)의 off 명령**
# 에서만 낸다. one-shot(duration_ms 붙은 단건)은 종료 ACK 자체가 없으므로 started 만.
# commands 행에는 pair_id 가 없고 source_id(=schedules.id)만 있어서 한 번 조회한다.
# _off 명령은 드물고 결과를 캐싱하므로 비용은 무시할 만하다.

SPAN_TTL_SEC = 600.0
_span_lock = threading.Lock()
_span_cache: dict[str, tuple[float, bool]] = {}     # schedule_id → (expires, is_span)


def _is_span_schedule(schedule_id: str) -> bool:
    """해당 예약이 구간 예약(pair_id 보유)인가. 조회 실패면 False(=started 로 폴백)."""
    now = time.monotonic()
    with _span_lock:
        hit = _span_cache.get(schedule_id)
        if hit and hit[0] > now:
            return hit[1]
    sb = get_supabase_client()
    try:
        res = (
            sb.table("schedules")
            .select("pair_id")
            .eq("id", schedule_id)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("schedules 조회 실패 (id=%s) — started 로 처리", schedule_id)
        return False
    rows = res.data or []
    is_span = bool(rows and rows[0].get("pair_id"))
    with _span_lock:
        _span_cache[schedule_id] = (time.monotonic() + SPAN_TTL_SEC, is_span)
    return is_span


def reset_span_cache() -> None:
    """테스트용."""
    with _span_lock:
        _span_cache.clear()


def _resolve_phase(action: str | None, source_id: str | None, succeeded: bool) -> str:
    """started | ended | failed."""
    if not succeeded:
        return "failed"
    if action and action.endswith("_off") and source_id and _is_span_schedule(source_id):
        return "ended"
    return "started"


_PHASE_EVENT = {
    "started": EVENT_STARTED,
    "ended": EVENT_ENDED,
    "failed": EVENT_FAILED,
}


def build_command_event(
    command_row: dict[str, Any], device_key: str, device_meta: dict[str, Any] | None
) -> dict[str, Any] | None:
    """acked 된 commands 행 → 앱 계약 이벤트 본문. 발행 대상이 아니면 None.

    command_row 는 handle_ack 의 UPDATE 응답(갱신된 행 전체)을 그대로 받는다.
    postgrest 가 return=representation 이 기본이라 JOIN 없이 아래가 다 들어있다.
    """
    source = command_row.get("source")
    if source not in _sources():
        return None

    command_id = command_row.get("id")
    if not command_id:
        return None

    result = command_row.get("result")
    succeeded = result == RESULT_OK
    phase = _resolve_phase(command_row.get("action"), command_row.get("source_id"), succeeded)
    event_type = _PHASE_EVENT[phase]

    meta = device_meta or {}
    # issued_by 는 NULL 허용이라 devices.owner_id 로 폴백 (회신 §2.2).
    user_id = command_row.get("issued_by") or meta.get("owner_id")
    if not user_id:
        logger.warning("push: user_id 확보 실패 command=%s — 건너뜀", command_id)
        return None

    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"command:{command_id}:{phase}",
        "type": event_type,
        "occurred_at": _now().isoformat(),
        "user_id": user_id,
        "payload": {
            "command_id": command_id,
            "device_id": command_row.get("device_id"),   # UUID
            "device_key": device_key,                     # terra-xxxxxxxx (MQTT client_id)
            "enclosure_id": meta.get("enclosure_id"),
            "schedule_id": command_row.get("source_id"),
            "execution_source": source,
            "execution_phase": phase,
            "action": command_row.get("action"),
            "outcome": "succeeded" if succeeded else "failed",
            "result": result,                             # 펌웨어 원문 (ok/busy/…)
            "device_name": meta.get("name"),
        },
    }


def enqueue(event: dict[str, Any]) -> bool:
    """outbox INSERT. 이미 같은 event_id 가 있으면 조용히 무시(멱등)."""
    if not _enabled():
        return False
    sb = get_supabase_client()
    try:
        sb.table("push_outbox").insert({
            "event_id": event["event_id"],
            "event_type": event["type"],
            "body": event,
        }).execute()
    except Exception as exc:  # noqa: BLE001 — supabase-py 예외 타입 넓음
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            return False        # 같은 이벤트 재적재 — 정상
        logger.exception("push_outbox INSERT 실패 (event=%s)", event["event_id"])
        return False
    logger.info("push 이벤트 적재: %s", event["event_id"])
    return True


def enqueue_command_failure(
    command_row: dict[str, Any],
    device_key: str,
    device_meta: dict[str, Any] | None,
    result: str,
) -> bool:
    """ACK 없이 끝난 명령을 실패 이벤트로 적재 (앱 회신 2026-09-16 §3-6, 추가확인).

    대상 세 가지 — 전부 `device.action.failed` 로 나간다.
      - `expired`       : TTL 초과로 발행조차 못 함
      - `unknown_device`: 미등록 기기
      - `no_ack`        : 발행했는데 응답이 영영 안 옴 (무인 실행에서 가장 중요한 알림)
    """
    row = dict(command_row)
    row["result"] = result          # 어느 경우든 ok 가 아니므로 failed 로 귀결
    event = build_command_event(row, device_key, device_meta)
    if event is None:
        return False
    return enqueue(event)


# ---------- 전송 (워커 스레드) ----------


def _backoff_sec(attempts: int) -> int:
    return min(MAX_BACKOFF_SEC, 2 ** min(attempts, 10) * 5)


def _post(client: httpx.Client, body: dict[str, Any]) -> httpx.Response:
    return client.post(
        _ingest_url(),
        headers={
            "Authorization": f"Bearer {_ingest_secret()}",
            "Content-Type": "application/json",
        },
        content=json.dumps(body, ensure_ascii=False).encode(),
        timeout=DEFAULT_TIMEOUT_SEC,
    )


def _mark(sb: Any, row_id: str, patch: dict[str, Any]) -> None:
    try:
        sb.table("push_outbox").update(patch).eq("id", row_id).execute()
    except Exception:  # noqa: BLE001
        logger.exception("push_outbox 상태 갱신 실패 (id=%s)", row_id)


def _handle_failure(sb: Any, row: dict[str, Any], reason: str, permanent: bool) -> None:
    """4xx 는 즉시 포기(앱 계약), 그 외는 지수 백오프 재시도."""
    attempts = int(row.get("attempts") or 0) + 1
    # last_error 에 본문을 통째로 넣지 않는다 — secret 은 헤더뿐이지만 방어적으로 자름.
    reason = reason[:300]
    if permanent:
        _mark(sb, row["id"], {"status": "failed", "attempts": attempts, "last_error": reason})
        logger.error("push 전송 영구 실패 event=%s: %s", row.get("event_id"), reason)
        return
    if attempts >= MAX_ATTEMPTS:
        _mark(sb, row["id"], {"status": "abandoned", "attempts": attempts, "last_error": reason})
        logger.error("push 전송 포기(%d회) event=%s: %s", attempts, row.get("event_id"), reason)
        return
    retry_at = _now() + timedelta(seconds=_backoff_sec(attempts))
    _mark(sb, row["id"], {
        "attempts": attempts,
        "next_retry_at": retry_at.isoformat(),
        "last_error": reason,
    })
    logger.warning(
        "push 전송 실패(%d회) event=%s → %s 재시도: %s",
        attempts, row.get("event_id"), retry_at.isoformat(), reason,
    )


def send_pending(batch: int = DEFAULT_BATCH) -> int:
    """전송 대기 이벤트를 보낸다. 보낸(=2xx) 건수 반환."""
    if not _enabled():
        return 0
    sb = get_supabase_client()
    try:
        res = (
            sb.table("push_outbox")
            .select("id, event_id, body, attempts")
            .eq("status", "pending")
            .lte("next_retry_at", _now().isoformat())
            .order("created_at", desc=False)
            .limit(batch)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("push_outbox 조회 실패")
        return 0

    rows = res.data or []
    if not rows:
        return 0

    sent = 0
    with httpx.Client() as client:
        for row in rows:
            body = row.get("body")
            if not isinstance(body, dict):
                _handle_failure(sb, row, "body 가 dict 아님", permanent=True)
                continue
            try:
                resp = _post(client, body)
            except Exception as exc:  # noqa: BLE001 — 네트워크 계열 전부 재시도 대상
                _handle_failure(sb, row, f"{type(exc).__name__}: {exc}", permanent=False)
                continue

            if 200 <= resp.status_code < 300:
                _mark(sb, row["id"], {
                    "status": "sent",
                    "attempts": int(row.get("attempts") or 0) + 1,
                    "sent_at": _now().isoformat(),
                })
                sent += 1
                continue

            # 4xx = 인증/payload 문제. 앱 계약상 무한 재시도하지 않는다.
            permanent = 400 <= resp.status_code < 500
            _handle_failure(
                sb, row, f"HTTP {resp.status_code}: {resp.text[:200]}", permanent=permanent
            )
    return sent


class PushOutboxWorker:
    """별도 스레드에서 주기 전송. ScheduleRunner 와 동일 라이프사이클."""

    def __init__(self, interval_sec: float = DEFAULT_INTERVAL_SEC) -> None:
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        if not _enabled():
            logger.info(
                "push outbox 워커 비활성 "
                "(PUSH_EVENT_INGEST_URL / PUSH_EVENT_INGEST_SECRET 미설정)"
            )
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="push-outbox")
        self._thread.start()
        logger.info("push outbox 워커 시작 (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
            logger.info("push outbox 워커 정지")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                n = send_pending()
                if n:
                    logger.info("push outbox: %d 건 전송", n)
            except Exception:  # noqa: BLE001
                logger.exception("push outbox 전송 루프 실패")
            self._stop.wait(self._interval)


__all__ = [
    "EVENT_ENDED",
    "EVENT_FAILED",
    "EVENT_STARTED",
    "NO_ACK_RESULT",
    "PushOutboxWorker",
    "build_command_event",
    "enqueue",
    "enqueue_command_failure",
    "reset_span_cache",
    "send_pending",
]
