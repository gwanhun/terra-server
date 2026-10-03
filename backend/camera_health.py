"""
카메라 진단값 이력 (2026-09-29, 설계 specs/camera-reliability-2026-09.md P1).

## 문제
카메라 heartbeat 의 clip_stats(업로드 성공/실패·SD 적체·마지막 녹화·last_err)와 sys(uptime·reset)는
cameras 행에 **최신값으로 덮어써진다.** 베타 카메라의 PANIC·워치독 재시작과 업로드 실패 추세는
누가 우연히 그 순간 조회하지 않는 한 전부 사라졌다(현장 테스트 A 의 PANIC 도 우연히 발견).

## 동작
camera_health_events 에 append-only 로 남긴다. 15초 heartbeat 를 전부 쓰지 않는다
(23대 × 5,760/일 — petcam Supabase Disk IO 예산 소진 전례).
- reset:    새 부팅을 보면 즉시 1행. 프로세스 안에서는 uptime 감소로 판정(늦게 처리된 heartbeat 에
            흔들리지 않게). 브리지가 꺼진 동안의 재부팅은 카메라를 처음 볼 때 DB 마지막 행의 부팅 시각
            (at - uptime_s)과 비교해 잡는다(카메라당 프로세스 1회 조회).
- snapshot: 카메라당 SNAPSHOT_INTERVAL_SEC 마다 1행. reset 행도 한 번의 기록으로 쳐서 타이머를 다시 잰다.

heartbeat UPDATE 와 분리된 별도 INSERT 이고 실패는 로그만 남긴다 — 선택 기록 때문에 온라인 표시가
죽은 9/17·9/21 사고 구조를 반복하지 않는다. 보존 30일은 migration 의 pg_cron 이 지운다.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)

TABLE = "camera_health_events"
SNAPSHOT_INTERVAL_SEC = 600       # owner 결정 2026-09-29: 10분
BOOT_TOLERANCE_SEC = 30           # 부팅 시각 추정 오차(전송 지연·시계 드리프트) 허용
MISSING_TABLE_TTL_SEC = 600.0     # 테이블 없음(migration 미적용) 이면 이 시간 동안 쓰기 중단

# PGRST205: Could not find the table ... in the schema cache / 42P01: relation ... does not exist
_MISSING_TABLE_CODES = ("PGRST205", "42P01")


class _CamState:
    __slots__ = ("prev_uptime", "last_write_ts")

    def __init__(self) -> None:
        self.prev_uptime: float | None = None    # 직전 heartbeat uptime — 프로세스 안 재시작 판정
        self.last_write_ts: float | None = None


_state: dict[str, _CamState] = {}
_paused_until = 0.0
_lock = threading.Lock()


def reset() -> None:
    global _paused_until
    with _lock:
        _state.clear()
        _paused_until = 0.0


def _parse_ts(raw: Any) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _boot_from_history(sb: Any, camera_uuid: str) -> float | None:
    """DB 마지막 행의 부팅 시각(at - uptime_s). 없거나 조회 실패면 None."""
    try:
        res = (
            sb.table(TABLE)
            .select("at, uptime_s")
            .eq("camera_id", camera_uuid)
            .order("at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 — 진단 기록용 조회. 실패해도 heartbeat 와 무관
        logger.warning("camera_health 이력 조회 실패 (camera=%s) — 이번 부팅은 스냅샷으로만 기록",
                       camera_uuid, exc_info=True)
        return None
    rows = res.data if isinstance(res.data, list) else []
    if not rows or not isinstance(rows[0], dict):
        return None
    at = _parse_ts(rows[0].get("at"))
    uptime = rows[0].get("uptime_s")
    if at is None or not isinstance(uptime, (int, float)):
        return None
    return at - float(uptime)


def _row(camera_uuid: str, kind: str, now: float, sys_state: dict[str, Any] | None,
         clips: dict[str, Any] | None, fw: str | None) -> dict[str, Any]:
    s = sys_state or {}
    c = clips or {}
    return {
        "camera_id": camera_uuid,
        "at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "kind": kind,
        "uptime_s": s.get("uptime_s"),
        "reset": s.get("reset"),
        "heap": s.get("heap"),
        "rssi": s.get("rssi"),
        "fw": fw,
        "up_ok": c.get("up_ok"),
        "up_fail": c.get("up_fail"),
        "sd_backlog": c.get("sd_backlog"),
        "last_rec_s": c.get("last_rec_s"),
        "last_err": c.get("last_err"),
        "stats": clips,
    }


def record(sb: Any, camera_uuid: str, sys_state: dict[str, Any] | None,
           clips: dict[str, Any] | None, fw: str | None, now: float | None = None) -> None:
    """heartbeat 1건을 보고 필요하면 reset/snapshot 1행 INSERT. 예외를 올리지 않는다."""
    global _paused_until
    now = time.time() if now is None else now
    if sys_state is None and not isinstance(clips, dict):
        return
    if now < _paused_until:
        return

    uptime = sys_state.get("uptime_s") if sys_state else None
    has_uptime = isinstance(uptime, (int, float))

    with _lock:
        st = _state.get(camera_uuid)
    rebooted = False
    if st is None:
        st = _CamState()
        # 프로세스에서 처음 보는 카메라 — 브리지가 꺼진 동안의 재부팅은 DB 마지막 행의 부팅 시각과 비교.
        if has_uptime:
            hist_boot = _boot_from_history(sb, camera_uuid)
            rebooted = (hist_boot is not None
                        and now - float(uptime) > hist_boot + BOOT_TOLERANCE_SEC)
    elif has_uptime and st.prev_uptime is not None:
        # 프로세스 안에서는 uptime 감소만 재시작으로 본다. 부팅 시각 비교는 브리지가 heartbeat 를
        # 늦게 처리하면(DB 지연) 밀려 보여 가짜 재시작을 쓴다(리뷰 09-29).
        rebooted = uptime < st.prev_uptime
    if has_uptime:
        st.prev_uptime = float(uptime)

    kind: str | None = None
    if rebooted:
        kind = "reset"
    elif st.last_write_ts is None or now - st.last_write_ts >= SNAPSHOT_INTERVAL_SEC:
        kind = "snapshot"
    if kind is not None:
        # 실패해도 타이머는 넘긴다 — 지속 장애 때 15초마다 재시도하며 부하를 키우지 않게.
        st.last_write_ts = now
    with _lock:
        _state[camera_uuid] = st
    if kind is None:
        return

    try:
        sb.table(TABLE).insert(_row(camera_uuid, kind, now, sys_state, clips, fw)).execute()
    except APIError as exc:
        if exc.code in _MISSING_TABLE_CODES:
            _paused_until = now + MISSING_TABLE_TTL_SEC
            logger.warning("%s 테이블 없음 — migration 미적용? %d초간 기록 중단", TABLE,
                           MISSING_TABLE_TTL_SEC)
        else:
            logger.exception("%s INSERT 거부 (camera=%s kind=%s)", TABLE, camera_uuid, kind)
    except Exception:  # noqa: BLE001 — 네트워크/타임아웃. heartbeat 는 이미 끝났으니 로그만
        logger.exception("%s INSERT 실패 (camera=%s kind=%s)", TABLE, camera_uuid, kind)
    else:
        if kind == "reset":
            logger.warning("camera %s: 재시작 기록 reset=%s uptime=%s",
                           camera_uuid, (sys_state or {}).get("reset"), uptime)
