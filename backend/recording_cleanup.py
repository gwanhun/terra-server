"""
라이브 직접 녹화(`live_recordings`) — 24시간 넘은 `pending` 정리 (Stage L).

## 왜
앱이 등록(POST /recordings)만 하고 업로드·complete 를 못 마치면 `pending` 행과 반쯤 올라간
R2 객체가 남는다. 한도(50개)에 `pending` 도 세므로 안 지우면 사용자가 한도에 갇힌다.

## 경합 (complete 와 동시에 돌 때)
행을 **먼저** 조건부(`status='pending'`)로 지우고, 실제로 지워진 행의 객체만 지운다.
그 사이 complete 가 `ready` 로 바꿨으면 0행 삭제 → 객체도 건드리지 않는다.
uvicorn 워커가 여럿이라 스레드가 중복돼도 같은 이유로 멱등(두 번째는 0행).

## 고아 객체 스캔 (앱 3차 회신 6번)
행 없이 R2 에만 남는 객체가 생기는 길이 있다 — 업로드 도중 DELETE(삭제 뒤 PUT 완료), R2 삭제
실패(로그만 남김), complete 에서 버린 썸네일 삭제 실패, 계정 삭제 cascade. 녹화 경로는 lifecycle
규칙도 없어(2026-10-20 대시보드 확인) 무기한 쌓인다.
→ ORPHAN_SCAN_EVERY 마다 `terra-clips/recordings/` 를 훑어 **ORPHAN_MIN_AGE 넘었고 어떤 행의
r2_key·thumbnail_key 도 아닌** 객체를 지운다. 행은 항상 PUT URL 발급 전에 생기고 URL 은 30분이면
만료되므로, 1시간 넘었는데 참조하는 행이 없는 객체는 고아다(정상 녹화를 지울 일이 없다).

구조는 backend/live_session.py `LiveSessionReaper` 와 같다.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

TABLE = "live_recordings"
PENDING_TTL = timedelta(hours=24)
CLEANUP_INTERVAL_SEC = 3600.0
PURGE_BATCH_LIMIT = 200

ORPHAN_PREFIX = "terra-clips/recordings/"
ORPHAN_MIN_AGE = timedelta(hours=1)
ORPHAN_SCAN_EVERY = timedelta(hours=6)
ORPHAN_BATCH_LIMIT = 1000
_ID_CHUNK = 100
# terra-clips/recordings/{user_id}/{camera_id}/{recording_id}.{mp4|jpg}
_KEY_ID_RE = re.compile(r"/(?P<rid>[0-9a-f-]{36})\.(?:mp4|jpg)$")


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


def purge_orphan_objects(
    sb: Any,
    now: datetime,
    list_fn: Callable[[str], Iterable[tuple[str, datetime]]],
    delete_fn: Callable[[str], None],
) -> int:
    """ORPHAN_MIN_AGE 넘었고 어떤 행도 참조하지 않는 녹화 객체를 최대 ORPHAN_BATCH_LIMIT 개 삭제. 지운 수."""
    cutoff = now - ORPHAN_MIN_AGE
    candidates: dict[str, str] = {}             # key → recording_id
    for key, modified in list_fn(ORPHAN_PREFIX):
        m = _KEY_ID_RE.search(key)
        if m is None:
            logger.warning("recording 고아 스캔: 형식 밖 키 건너뜀 key=%s", key)
            continue
        if modified < cutoff:
            candidates[key] = m.group("rid")
        if len(candidates) >= ORPHAN_BATCH_LIMIT:
            break

    referenced: set[str] = set()
    ids = sorted(set(candidates.values()))
    for i in range(0, len(ids), _ID_CHUNK):
        res = (sb.table(TABLE).select("id, r2_key, thumbnail_key")
               .in_("id", ids[i:i + _ID_CHUNK]).execute())
        for row in res.data if isinstance(res.data, list) else []:
            if isinstance(row, dict):
                referenced.update(k for k in (row.get("r2_key"), row.get("thumbnail_key")) if k)

    purged = 0
    for key in candidates:
        if key in referenced:
            continue
        try:
            delete_fn(key)
            purged += 1
        except (BotoCoreError, ClientError):
            logger.error("recording r2 delete failed key=%s", key)
    if purged:
        logger.info("recording 고아 객체 정리: %d개", purged)
    return purged


class PendingRecordingReaper:
    """API 프로세스 백그라운드 스레드 — interval_sec 마다 purge_stale_pending,
    list_fn 이 있으면 ORPHAN_SCAN_EVERY 마다 purge_orphan_objects 도."""

    def __init__(self, sb_factory: Callable[[], Any], delete_fn: Callable[[str], None],
                 interval_sec: float = CLEANUP_INTERVAL_SEC,
                 list_fn: Callable[[str], Iterable[tuple[str, datetime]]] | None = None) -> None:
        self._sb_factory = sb_factory
        self._delete_fn = delete_fn
        self._interval = interval_sec
        self._list_fn = list_fn
        self._last_orphan_scan: datetime | None = None
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
            now = datetime.now(timezone.utc)
            try:
                purge_stale_pending(self._sb_factory(), now, self._delete_fn)
            except Exception:  # noqa: BLE001 — 한 번 실패해도 다음 주기에 다시
                logger.exception("recording pending 정리 실패")
            if self._list_fn is None:
                continue
            if self._last_orphan_scan is not None and now - self._last_orphan_scan < ORPHAN_SCAN_EVERY:
                continue
            self._last_orphan_scan = now
            try:
                purge_orphan_objects(self._sb_factory(), now, self._list_fn, self._delete_fn)
            except Exception:  # noqa: BLE001 — 한 번 실패해도 다음 주기에 다시
                logger.exception("recording 고아 객체 정리 실패")


__all__ = [
    "CLEANUP_INTERVAL_SEC",
    "ORPHAN_MIN_AGE",
    "ORPHAN_PREFIX",
    "ORPHAN_SCAN_EVERY",
    "PENDING_TTL",
    "PendingRecordingReaper",
    "purge_one",
    "purge_orphan_objects",
    "purge_stale_pending",
]
