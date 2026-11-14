"""카메라 네트워크 신호 약함 안내 (Stage V, specs/stage-v-camera-weak-signal-notice.md).

카메라 heartbeat(15초)의 `wifi_rssi` 가 -75dBm 이하로 10분 넘게 이어지면 그 카메라 화면
막대에 안내를 띄우고(app_notifications 직접 INSERT — 푸시 없음), -70dBm 이상으로
회복하면 내린다(read_at 채움).

왜 메모리 상태: 지속 판정은 브리지 프로세스 1개·수신 스레드 1개에서만 돌아서 락도
이력 테이블도 필요 없다. 재시작하면 10분을 다시 셀 뿐이고, 24시간 제한은 발송 직전
DB 조회로 지킨다.
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

import httpx
from postgrest.exceptions import APIError
from supabase import Client

logger = logging.getLogger(__name__)

WEAK_DBM = -75          # 이 값 이하면 약함
RECOVER_DBM = -70       # 이 값 이상이면 회복(안내 내림)
STREAK_BREAK_DBM = -71  # 이 값 "초과"(= 회복 기준 이상) 샘플만 약함 지속을 끊는다.
                        # -74~-71 은 RSSI 흔들림으로 보고 지속을 끊지 않는다
HOLD_SEC = 600          # 약함이 이만큼 이어지면 발송
GAP_RESET_SEC = 300     # 약함 샘플 공백이 이보다 길면 약함 지속을 처음부터.
                        # 신호가 제일 나쁜 카메라는 heartbeat 가 몇 분씩 빠진다
RETRY_SEC = 600         # 발송 시도 뒤 다음 시도까지
CLEAR_RETRY_SEC = 60    # 해제 실패 뒤 다음 해제 시도까지
RSSI_MIN_DBM = -120     # 이 범위 밖(0·양수 포함)은 잘못된 보고값으로 보고 무시
RSSI_MAX_DBM = -1
REPEAT_SEC = 86400      # 카메라당 24시간에 1회

# 앱 계약 (docs/APP_CAMERA_WEAK_SIGNAL_2026-11-13.md)
KIND = "camera.weak_signal"
TITLE = "네트워크 신호가 약하니 점검해 주세요"
BODY = "공유기와 카메라 사이 거리나 장애물을 확인해 주세요"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_DB_ERRORS = (APIError, httpx.HTTPError)


class _State:
    __slots__ = ("weak_since", "last_weak_at", "next_try", "next_clear", "active")

    def __init__(self) -> None:
        self.weak_since: float | None = None   # 약함 시작
        self.last_weak_at: float = 0.0         # 마지막 약함 샘플
        self.next_try: float = 0.0             # 다음 발송 시도 가능 시각
        self.next_clear: float = 0.0           # 다음 해제 시도 가능 시각
        self.active: bool | None = None        # None=모름, True=안내 떠 있음, False=없음


class WeakTracker:
    """카메라별 "약함 지속" 판정. 시계를 읽지 않는다 — `now`(단조 시각, 초)는 호출자가 준다.

    락 없음: MQTT 수신 스레드 1개에서만 불린다.
    """

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}

    def _state(self, camera_uuid: str) -> _State:
        st = self._states.get(camera_uuid)
        if st is None:
            st = self._states[camera_uuid] = _State()
        return st

    def observe(self, camera_uuid: str, rssi: int, now: float) -> Literal["send", "clear"] | None:
        st = self._state(camera_uuid)
        if rssi <= WEAK_DBM:
            if st.weak_since is None or now - st.last_weak_at > GAP_RESET_SEC:
                st.weak_since = now
            st.last_weak_at = now
            if now - st.weak_since >= HOLD_SEC and now >= st.next_try:
                st.next_try = now + RETRY_SEC
                return "send"
            return None
        if rssi > STREAK_BREAK_DBM:
            st.weak_since = None
        if rssi >= RECOVER_DBM and st.active is not False and now >= st.next_clear:
            st.active = False
            return "clear"
        return None

    def mark_sent(self, camera_uuid: str, now: float) -> None:
        st = self._state(camera_uuid)
        st.active = True
        st.next_try = now + REPEAT_SEC

    def set_active(self, camera_uuid: str, value: bool | None) -> None:
        """`active` 만 바꾼다(`next_try` 는 그대로)."""
        self._state(camera_uuid).active = value

    def clear_failed(self, camera_uuid: str, now: float) -> None:
        """해제가 실패했다 — `CLEAR_RETRY_SEC` 뒤 회복 샘플에서 다시 시도하게 한다."""
        st = self._state(camera_uuid)
        st.active = None
        st.next_clear = now + CLEAR_RETRY_SEC

    def reset(self) -> None:
        self._states.clear()


# ---------- 스위치 ----------

def enabled() -> bool:
    """`NET_WEAK_NOTICE_ENABLED` — 기본 꺼짐. true/1/yes/on 만 켠다."""
    return (os.getenv("NET_WEAK_NOTICE_ENABLED") or "").strip().lower() in _TRUE


def dry_run() -> bool:
    """`NET_WEAK_NOTICE_DRY_RUN` — 기본 **켜짐**. 0/false/no/off 만 끈다(오타는 안전한 쪽)."""
    return (os.getenv("NET_WEAK_NOTICE_DRY_RUN") or "").strip().lower() not in _FALSE


# ---------- 안내 넣기·내리기 (app_notifications) ----------

def _owner(sb: Client, camera_uuid: str) -> str | None:
    """활성 카메라의 소유자. 행이 없거나 해제(unlinked)·이전(superseded)된 행이면 None."""
    rows = (
        sb.table("cameras")
        .select("owner_id, superseded_at, unlinked_at")
        .eq("id", camera_uuid)
        .limit(1)
        .execute()
        .data
    ) or []
    if not rows or rows[0].get("superseded_at") or rows[0].get("unlinked_at"):
        return None
    return rows[0].get("owner_id") or None


def _of_camera(rows: list[dict[str, Any]] | None, camera_uuid: str) -> list[dict[str, Any]]:
    """`data.camera_id` 로 거른다 — JSON 경로 필터 대신 Python 에서(FakeSB·PostgREST 같은 코드)."""
    return [
        r for r in rows or []
        if isinstance(r.get("data"), dict) and r["data"].get("camera_id") == camera_uuid
    ]


def _unread(sb: Client, owner: str, camera_uuid: str) -> list[dict[str, Any]]:
    """이 카메라의 안 읽은 안내. limit 없음 — 다른 카메라 행에 밀려 안 보이면 안 된다."""
    rows = (
        sb.table("app_notifications")
        .select("id, data")
        .eq("user_id", owner)   # service_role 은 RLS 바이패스 → 소유자 필터 명시
        .eq("kind", KIND)
        .is_("read_at", "null")
        .execute()
        .data
    )
    return _of_camera(rows, camera_uuid)


def send_notice(
    sb: Client, camera_uuid: str, rssi: int
) -> Literal["queued", "recent", "unread", "no_owner", "dry_run", "failed"]:
    """안내 1건 넣기. 안 읽은 안내가 있거나 24시간 안에 보낸 적 있으면 넣지 않는다."""
    try:
        owner = _owner(sb, camera_uuid)
        if owner is None:
            return "no_owner"
        if _unread(sb, owner, camera_uuid):
            return "unread"
        since = (datetime.now(timezone.utc) - timedelta(seconds=REPEAT_SEC)).isoformat()
        recent = (
            sb.table("app_notifications")
            .select("id, data")
            .eq("user_id", owner)
            .eq("kind", KIND)
            .gte("created_at", since)
            .execute()
            .data
        )
        if _of_camera(recent, camera_uuid):
            return "recent"
        if dry_run():
            logger.info("net-weak intent camera=%s rssi=%s", camera_uuid, rssi)
            return "dry_run"
        event_id = str(uuid4())
        sb.table("app_notifications").insert({
            "user_id": owner,
            "kind": KIND,
            "category": "camera",
            "title": TITLE,
            "body": BODY,
            "route": "/crecam",
            "data": {"kind": KIND, "source": "external", "camera_id": camera_uuid, "rssi": rssi},
            "source": "external",
            "source_event_id": event_id,
            "dedupe_key": "net-weak:" + camera_uuid + ":" + event_id,
        }).execute()
    except _DB_ERRORS:
        logger.exception("net-weak 안내 넣기 실패 (camera=%s)", camera_uuid)
        return "failed"
    return "queued"


def clear_notices(sb: Client, camera_uuid: str) -> int | None:
    """이 카메라의 안 읽은 안내를 읽음 처리. 처리한 행 수, DB 오류면 None. 드라이런은 0."""
    if dry_run():
        return 0
    try:
        owner = _owner(sb, camera_uuid)
        if owner is None:
            return 0
        ids = [r["id"] for r in _unread(sb, owner, camera_uuid)]
        if not ids:
            return 0
        res = (
            sb.table("app_notifications")
            .update({"read_at": datetime.now(timezone.utc).isoformat()})
            .eq("user_id", owner)
            .in_("id", ids)
            .execute()
        )
    except _DB_ERRORS:
        logger.exception("net-weak 안내 내리기 실패 (camera=%s)", camera_uuid)
        return None
    return len(res.data or [])


# ---------- heartbeat 연결 ----------

@lru_cache(maxsize=1)
def get_tracker() -> WeakTracker:
    """브리지 프로세스에 하나뿐인 트래커(`@lru_cache` 싱글톤)."""
    return WeakTracker()


def on_camera_rssi(sb: Client, camera_uuid: str, camera_id_text: str, rssi: int) -> None:
    """카메라 heartbeat 마다(RSSI 가 있을 때) 불린다. 스위치가 꺼져 있으면 아무것도 안 한다."""
    if not enabled():
        return
    if not RSSI_MIN_DBM <= rssi <= RSSI_MAX_DBM:
        return   # 0·양수 같은 잘못된 값을 회복으로 보면 막대가 잘못 내려간다
    tracker = get_tracker()
    now = time.monotonic()
    action = tracker.observe(camera_uuid, rssi, now)
    if action == "send":
        result = send_notice(sb, camera_uuid, rssi)
        if result in ("queued", "dry_run"):
            # 24시간 동안 다시 조회하지 않는다. 그 밖의 결과는 10분(RETRY_SEC) 뒤 다시 조회 —
            # 여기서 mark_sent 를 하면 직전 안내의 24시간이 끝난 뒤에도 최대 24시간 더 막힌다.
            tracker.mark_sent(camera_uuid, now)
        elif result == "unread":
            tracker.set_active(camera_uuid, True)   # 회복 때 내릴 수 있게
        if result == "queued":
            logger.info("net-weak sent camera=%s rssi=%s", camera_id_text, rssi)
    elif action == "clear":
        try:
            n = clear_notices(sb, camera_uuid)
        except Exception:
            # observe 가 이미 "안내 없음"으로 바꿔 놨다. 되돌려 놓지 않으면 다시 시도하지 않는다.
            tracker.clear_failed(camera_uuid, now)
            raise
        if n is None:
            tracker.clear_failed(camera_uuid, now)  # DB 오류 — 60초 뒤 회복 샘플에서 다시
        elif n >= 1:
            logger.info("net-weak cleared camera=%s n=%d", camera_id_text, n)
