"""
라이브 시청 제한 (2026-09-29 owner 결정, specs/live-view-limit.md).

## 왜
라이브를 오래 켜두면 카메라 WebRTC 루프가 고착돼 영상이 멈추고(stalled), 앱이 재연결을 반복하면서
악화 → 펌웨어 워치독 재부팅(SW:rtc_loop_stall). 두 기기 동시 시청 때도 카메라가 40초 먹통이었다.
녹화가 본업인 카메라를 보호하려고 라이브를 짧게 끊는다.

## 규칙 (서버 강제 — 업데이트 안 한 앱도 적용)
- 한 번에 LIVE_MAX_SEC(15분). 시계는 **카메라 기준 연속 시청 시간**:
  같은 기기 재연결·다른 기기 가져오기·끊고 CONTINUE_GAP_SEC(60초) 안 재시작은 시계가 이어진다
  (기기를 번갈아 가져오거나 껐다 켜서 카메라가 쉬지 않고 라이브하는 우회 방지).
- 15분이 되면 서버가 세션을 닫고(expire_due) LIVE_COOLDOWN_SEC(5분) 동안 새 시청 거절.
- 한 번에 한 기기(viewer_id). 다른 기기면 LiveBusy(누가 보는지) → 앱이 "○○에서 시청 중. 연결할까요?"
  takeover=True 면 기존 세션을 닫고 가져온다. 끊긴 쪽은 cameras Realtime 으로 알게 된다.
- 구버전 앱(viewer_id 없음)끼리는 같은 시청자로 본다 — 한 기기 제한 없이 시간 제한만.

## 상태 저장
cameras 행의 live_* 컬럼(migrations/2026-09-29_cameras_live_session.sql) + 기존 stream_mode/stream_until.
앱이 cameras 를 Realtime 구독 중이라 세션 종료·가져오기가 DB 변경만으로 전달된다.
claim 은 프로세스 락 안에서 행을 다시 읽고 쓴다 — terra-api 는 단일 uvicorn 프로세스(ICE relay 와 같은 전제).
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

LIVE_MAX_SEC = 15 * 60
LIVE_COOLDOWN_SEC = 5 * 60
CONTINUE_GAP_SEC = 60
VIEWER_LABEL_MAX = 40
LEGACY_VIEWER = "legacy"
LEGACY_LABEL = "이전 버전 앱"
REAP_INTERVAL_SEC = 15.0

LIVE_COLUMNS = ("id, camera_id, live_session_id, live_viewer_id, live_viewer, live_started_at, "
                "live_ended_at, live_cooldown_until, stream_until")

_CLEAR = {"live_session_id": None, "live_viewer_id": None, "stream_mode": None, "stream_until": None}


class LiveCooldown(Exception):
    """15분 시청 후 쉬는 중. retry_after 초 뒤 가능."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"live cooldown {retry_after}s")
        self.retry_after = retry_after


class LiveBusy(Exception):
    """다른 기기가 시청 중."""

    def __init__(self, viewer: str, since: str | None) -> None:
        super().__init__(f"live in use by {viewer}")
        self.viewer = viewer
        self.since = since


@dataclass(frozen=True, slots=True)
class Claim:
    previous_session: str | None     # 닫아야 할 기존 세션(가져오기·같은 기기 새 세션)
    live_until: datetime


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(camera_uuid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(camera_uuid, threading.Lock())


def _ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _retry(until: datetime, now: datetime) -> int:
    return max(1, math.ceil((until - now).total_seconds()))


def claim(sb: Any, camera_uuid: str, owner_id: str, *, session_id: str, viewer_id: str | None,
          viewer_label: str | None, takeover: bool, now: datetime | None = None) -> Claim:
    """시청 시작(또는 이어가기)을 기록. 막히면 LiveCooldown/LiveBusy."""
    now = now or datetime.now(timezone.utc)
    vid = viewer_id or LEGACY_VIEWER
    label = (viewer_label or "").strip()[:VIEWER_LABEL_MAX] or (LEGACY_LABEL if not viewer_id else "다른 기기")

    with _lock_for(camera_uuid):
        data = (sb.table("cameras").select(LIVE_COLUMNS).eq("id", camera_uuid)
                .eq("owner_id", owner_id).limit(1).execute()).data
        cam = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}

        cooldown = _ts(cam.get("live_cooldown_until"))
        if cooldown and cooldown > now:
            raise LiveCooldown(_retry(cooldown, now))

        active_sid = cam.get("live_session_id")
        active_until = _ts(cam.get("stream_until"))
        if active_sid and active_until and active_until <= now:
            # 만료됐는데 스윕(15초 주기) 전 — 쉼 시간은 만료 시각부터
            raise LiveCooldown(_retry(active_until + timedelta(seconds=LIVE_COOLDOWN_SEC), now))
        active = bool(active_sid)

        previous: str | None = None
        taken_over = False
        if active and active_sid != session_id:
            if cam.get("live_viewer_id") != vid:
                if not takeover:
                    raise LiveBusy(cam.get("live_viewer") or LEGACY_LABEL, cam.get("live_started_at"))
                taken_over = True
            previous = active_sid

        started = _ts(cam.get("live_started_at"))
        if not active:
            ended = _ts(cam.get("live_ended_at"))
            recent = (started is not None and ended is not None and ended >= started
                      and (now - ended).total_seconds() < CONTINUE_GAP_SEC)
            if not recent:
                started = now
        started = started or now
        live_until = started + timedelta(seconds=LIVE_MAX_SEC)
        if live_until <= now:
            raise LiveCooldown(_retry(live_until + timedelta(seconds=LIVE_COOLDOWN_SEC), now))

        patch: dict[str, Any] = {
            "live_session_id": session_id,
            "live_viewer_id": vid,
            "live_viewer": label,
            "live_started_at": started.isoformat(),
            "stream_mode": "webrtc",
            "stream_until": live_until.isoformat(),
            "live_end_reason": "taken_over" if taken_over else None,
        }
        sb.table("cameras").update(patch).eq("id", camera_uuid).eq("owner_id", owner_id).execute()
    if taken_over:
        logger.info("live %s: %s 가 %s 세션 가져옴", camera_uuid, label, previous)
    return Claim(previous_session=previous, live_until=live_until)


def release(sb: Any, camera_uuid: str, owner_id: str, session_id: str, reason: str,
            now: datetime | None = None) -> bool:
    """이 세션이 활성일 때만 종료 기록(쉼 없음). 가져오기 당한 기기의 늦은 close 는 무시된다."""
    now = now or datetime.now(timezone.utc)
    res = (sb.table("cameras")
           .update({**_CLEAR, "live_ended_at": now.isoformat(), "live_end_reason": reason})
           .eq("id", camera_uuid).eq("owner_id", owner_id).eq("live_session_id", session_id)
           .execute())
    return isinstance(res.data, list) and bool(res.data)


def expire_due(sb: Any, close_fn: Callable[[str, str], None], now: datetime | None = None) -> int:
    """15분이 지난 세션을 닫고 5분 쉼을 건다. 닫은 개수. 카메라 close 실패해도 세션은 끝낸다."""
    now = now or datetime.now(timezone.utc)
    data = (sb.table("cameras").select(LIVE_COLUMNS).not_.is_("live_session_id", "null")
            .lt("stream_until", now.isoformat()).limit(200).execute()).data
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    closed = 0
    for cam in rows:
        sid = cam.get("live_session_id")
        if not sid:
            continue
        try:
            close_fn(cam.get("camera_id") or "", sid)
        except Exception:  # noqa: BLE001 — 카메라가 못 받아도 서버 상태는 정리(카메라도 자체 timeout)
            logger.warning("live %s: 15분 만료 close 발행 실패 — 서버 세션만 종료", cam.get("id"),
                           exc_info=True)
        res = (sb.table("cameras").update({
            **_CLEAR,
            "live_ended_at": now.isoformat(),
            "live_cooldown_until": (now + timedelta(seconds=LIVE_COOLDOWN_SEC)).isoformat(),
            "live_end_reason": "time_limit",
        }).eq("id", cam["id"]).eq("live_session_id", sid).execute())   # 그 사이 가져오기면 건드리지 않음
        if isinstance(res.data, list) and res.data:
            closed += 1
            logger.info("live %s: 15분 만료 → 세션 %s 종료, %d초 쉼", cam["id"], sid, LIVE_COOLDOWN_SEC)
    return closed


class LiveSessionReaper:
    """API 프로세스 백그라운드 스레드 — REAP_INTERVAL_SEC 마다 expire_due."""

    def __init__(self, sb_factory: Callable[[], Any], close_fn: Callable[[str, str], None],
                 interval_sec: float = REAP_INTERVAL_SEC) -> None:
        self._sb_factory = sb_factory
        self._close_fn = close_fn
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="live-session-reaper")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                expire_due(self._sb_factory(), self._close_fn)
            except Exception:  # noqa: BLE001
                logger.exception("live 세션 만료 스윕 실패")
