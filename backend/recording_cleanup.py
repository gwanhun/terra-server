"""
라이브 직접 녹화(`live_recordings`) — 24시간 넘은 `pending` 정리 (Stage L).

## 왜
앱이 등록(POST /recordings)만 하고 업로드·complete 를 못 마치면 `pending` 행과 반쯤 올라간
R2 객체가 남는다. 한도(50개)에 `pending` 도 세므로 안 지우면 사용자가 한도에 갇힌다.

## 경합 (complete 와 동시에 돌 때)
행을 **먼저** 조건부(`status='pending'`)로 지우고, 실제로 지워진 행의 객체만 지운다.
그 사이 complete 가 `ready` 로 바꿨으면 0행 삭제 → 객체도 건드리지 않는다.
uvicorn 워커가 여럿이라 스레드가 중복돼도 같은 이유로 멱등(두 번째는 0행).

구조는 backend/live_session.py `LiveSessionReaper` 와 같다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

TABLE = "live_recordings"
PENDING_TTL = timedelta(hours=24)
CLEANUP_INTERVAL_SEC = 3600.0
PURGE_BATCH_LIMIT = 200


def purge_one(sb: Any, row: dict[str, Any], delete_fn: Callable[[str], None]) -> bool:
    """`pending` 인 그 행을 지우고, 지워졌을 때만 영상·썸네일 객체를 지운다. 지웠으면 True."""
    res = (sb.table(TABLE).delete()
           .eq("id", row["id"]).eq("status", "pending").execute())
    deleted = res.data if isinstance(res.data, list) else []
    if not deleted:
        return False
    gone = deleted[0] if isinstance(deleted[0], dict) else {}
    for col in ("r2_key", "thumbnail_key"):
        key = gone.get(col) or row.get(col)
        if not key:
            continue
        try:
            delete_fn(key)
        except (BotoCoreError, ClientError):
            logger.error("recording r2 delete failed key=%s", key)
    return True


def purge_stale_pending(sb: Any, now: datetime, delete_fn: Callable[[str], None]) -> int:
    """`created_at < now - 24h` 인 `pending` 을 최대 PURGE_BATCH_LIMIT 행 정리. 지운 행 수."""
    cutoff = (now - PENDING_TTL).isoformat()
    res = (sb.table(TABLE).select("id, r2_key, thumbnail_key")
           .eq("status", "pending").lt("created_at", cutoff)
           .limit(PURGE_BATCH_LIMIT).execute())
    rows = [r for r in res.data if isinstance(r, dict)] if isinstance(res.data, list) else []
    purged = 0
    for row in rows:
        if purge_one(sb, row, delete_fn):
            purged += 1
    if purged:
        logger.info("recording pending 정리: %d행", purged)
    return purged


class PendingRecordingReaper:
    """API 프로세스 백그라운드 스레드 — interval_sec 마다 purge_stale_pending."""

    def __init__(self, sb_factory: Callable[[], Any], delete_fn: Callable[[str], None],
                 interval_sec: float = CLEANUP_INTERVAL_SEC) -> None:
        self._sb_factory = sb_factory
        self._delete_fn = delete_fn
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="recording-pending-reaper")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                purge_stale_pending(self._sb_factory(), datetime.now(timezone.utc), self._delete_fn)
            except Exception:  # noqa: BLE001 — 한 번 실패해도 다음 주기에 다시
                logger.exception("recording pending 정리 실패")


__all__ = [
    "CLEANUP_INTERVAL_SEC",
    "PENDING_TTL",
    "PendingRecordingReaper",
    "purge_one",
    "purge_stale_pending",
]
