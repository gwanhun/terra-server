"""
카메라 부분 멈춤 감지 (2026-09-29, 설계 specs/camera-reliability-2026-09.md P3).

## 문제
베타 카메라가 heartbeat 는 살아있는데 녹화·업로드만 멈추고, 사람이 재부팅해야 회복됐다. 서버는
이 상태를 warning 로그로만 남겨(handlers._warn_clip_regression) 아무도 몰랐다. offline_monitor 는
heartbeat 가 끊긴 경우만 본다.

## 동작 (서버가 이미 받는 heartbeat 값만 사용)
- camera_abnormal_reset: 새 부팅의 reset 사유가 크래시·워치독·펌웨어 자가 복구
  (PANIC · WDT · INT_WDT · TASK_WDT · SW:rtc_* · SW:mqtt_stuck · SW:net_wd · SW:boot_net_wd ·
  SW:upload_stuck · SW:cam_stall — 펌웨어 워치독은 이관훈 회신 09-29). 부팅당 1건.
  POWERON · BROWNOUT · SW:rotate · SW:mqtt_reboot 같은 정상/의도된 재시작은 제외.
- camera_upload_stalled: 업로드 성공이 STALL_SEC 넘게 멈춘 채 실패만 STALL_MIN_FAILS 이상 늘었다.
  성공 = up_ok + sd_ok(SD 적체 재업로드 성공), 실패 = up_fail + sd_fail. 구 펌웨어(fb2-p4 0.1.0,
  현장 C)는 up_ok 0 인 채 sd_ok 로 올라가서, up_ok 만 보면 정상 카메라를 정체로 오판한다. last_rec_s 는 쓰지 않는다 — 게코가 안 움직여 녹화가 없는 것과
  구분이 안 된다(현장 실측).
- 해제: 업로드가 다시 성공하면(성공 카운터 증가) 활성 알림을 resolved 처리.

camera_alerts 테이블(alerts 와 같은 모양, alerts.device_id 는 devices FK 라 카메라를 못 담는다)에 쓴다.
owner 결정 09-29: 알림 행만 — 사용자 푸시·자동 재부팅 없음(푸시는 앱 계약 후 별도).
DB 실패는 로그만 — heartbeat 처리를 막지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "camera_alerts"
KIND_RESET = "camera_abnormal_reset"
KIND_STALL = "camera_upload_stalled"
# 칩 크래시·워치독 + 펌웨어 자체 워치독(이관훈 회신 2026-09-29, 0.1.0/0.2.0 합집합).
# 0.1.0: rtc_loop_stall·mqtt_stuck·cam_stall / 0.2.0 추가: net_wd·boot_net_wd·upload_stuck.
# TASK_WDT 는 현 빌드에서 재부팅 안 하지만(PANIC 옵션 off) 켜지면 크래시라 포함.
ABNORMAL_RESETS = frozenset({
    "PANIC", "WDT", "INT_WDT", "TASK_WDT",
    "SW:rtc_loop_stall", "SW:mqtt_stuck", "SW:net_wd", "SW:boot_net_wd",
    "SW:upload_stuck", "SW:cam_stall",
})
# WebRTC 루프/송신/락 정지는 "rtc_loop_stall 등" 여러 사유라 이름이 확정 안 됨 → prefix 로.
ABNORMAL_RESET_PREFIXES = ("SW:rtc_",)


def is_abnormal_reset(reason: object) -> bool:
    return isinstance(reason, str) and (
        reason in ABNORMAL_RESETS or reason.startswith(ABNORMAL_RESET_PREFIXES))
STALL_SEC = 1800.0          # 업로드 성공이 이만큼 없고
STALL_MIN_FAILS = 3         # 그동안 실패가 이만큼 늘면 정체
FRESH_UPTIME_SEC = 120      # 브리지 재시작 후 첫 관측: 이보다 짧은 uptime 만 새 부팅으로 본다
BOOT_TOLERANCE_SEC = 30     # 부팅 시각 추정 오차 허용 (camera_health·schedule_restore 와 같은 방식)

_MESSAGES = {
    KIND_RESET: "카메라가 비정상 재시작했어요 ({reset})",
    KIND_STALL: "카메라 영상 업로드가 {minutes}분째 실패하고 있어요",
}


class _Cam:
    __slots__ = ("boot_ts", "prev_uptime", "ok_val", "ok_ts", "fail_base", "active", "resolve_checked",
                 "pending_reset")

    def __init__(self) -> None:
        self.boot_ts: float | None = None       # 현재 부팅 추정 시각 — DB 중복 확인(since)용
        self.prev_uptime: float | None = None   # 직전 heartbeat uptime — 새 부팅 판정용
        self.ok_val: int | None = None
        self.ok_ts = 0.0
        self.fail_base = 0
        self.active: set[str] = set()
        self.resolve_checked = False    # 프로세스 시작 후 DB 의 옛 활성 알림을 한 번 정리했나
        # 기록 못 한 재시작 알림 — 부팅 첫 heartbeat 에서만 판정되니 INSERT 실패 시 여기 두고 재시도
        self.pending_reset: dict[str, Any] | None = None


_cams: dict[str, _Cam] = {}
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _cams.clear()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _epoch(ts: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _raise_alert(sb: Any, st: _Cam, camera_uuid: str, label: str, kind: str,
                 context: dict[str, Any], now: float, since: float | None = None) -> None:
    """활성 알림이 없으면 INSERT. since 가 있으면 그 이후 생긴 활성 알림만 중복으로 본다
    (재시작 알림: 같은 부팅만 중복 — 회복 전 또 크래시하면 새 행)."""
    if kind in st.active:
        return
    rows = (
        sb.table(TABLE).select("id, triggered_at").eq("camera_id", camera_uuid).eq("kind", kind)
        .is_("resolved_at", "null").limit(20).execute()
    ).data
    existing = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if since is not None:
        cut = since - BOOT_TOLERANCE_SEC
        existing = [r for r in existing if (_epoch(r.get("triggered_at")) or cut) >= cut]
    if existing:
        st.active.add(kind)
        return      # 브리지 재시작 전 만든 활성 알림이 남아 있다
    sb.table(TABLE).insert({
        "camera_id": camera_uuid,
        "kind": kind,
        "severity": "warning",
        "message": _MESSAGES[kind].format(
            reset=context.get("reset"), minutes=int(context.get("stalled_sec", 0) // 60)),
        "context": context,
        "triggered_at": _iso(now),
    }).execute()
    st.active.add(kind)     # INSERT 성공 뒤에만 — 실패하면 다음 heartbeat 에 다시 시도
    logger.warning("camera %s: %s %s", label, kind, context)


def _resolve_all(sb: Any, st: _Cam, camera_uuid: str, label: str, now: float) -> None:
    kinds = set(st.active) if st.resolve_checked else {KIND_RESET, KIND_STALL}
    st.resolve_checked = True
    for kind in sorted(kinds):
        sb.table(TABLE).update({"resolved_at": _iso(now)}).eq("camera_id", camera_uuid) \
            .eq("kind", kind).is_("resolved_at", "null").execute()
        if kind in st.active:
            logger.info("camera %s: %s 회복(업로드 성공)", label, kind)
    st.active.clear()


def _evaluate(sb: Any, camera_uuid: str, label: str, sys_state: dict[str, Any] | None,
              clips: dict[str, Any] | None, now: float) -> None:
    with _lock:
        st = _cams.setdefault(camera_uuid, _Cam())

    uptime = (sys_state or {}).get("uptime_s")
    reason = (sys_state or {}).get("reset")
    new_boot = False
    if isinstance(uptime, (int, float)):
        # 새 부팅 = uptime 감소. 부팅 시각(now - uptime) 비교는 쓰지 않는다 — 브리지가 DB 지연으로
        # heartbeat 를 늦게 처리하면 부팅 시각이 밀려 보여 새 부팅으로 오판한다(리뷰 09-29).
        if st.prev_uptime is None:
            new_boot = uptime < FRESH_UPTIME_SEC      # 브리지 재시작 직후 첫 관측
        else:
            new_boot = uptime < st.prev_uptime
        st.prev_uptime = float(uptime)
        if st.boot_ts is None or new_boot:
            st.boot_ts = now - float(uptime)
    if new_boot:
        st.ok_val = None                     # 카운터가 0 부터 다시 센다 — 창 재시작
        st.active.discard(KIND_RESET)        # 새 부팅의 크래시는 새 알림
        st.pending_reset = ({"reset": reason, "uptime_s": uptime}
                            if is_abnormal_reset(reason) else None)
    if st.pending_reset is not None:
        _raise_alert(sb, st, camera_uuid, label, KIND_RESET, st.pending_reset, now,
                     since=st.boot_ts)
        st.pending_reset = None

    if not isinstance(clips, dict):
        return
    up_ok, up_fail = clips.get("up_ok"), clips.get("up_fail")
    if not isinstance(up_ok, int) or not isinstance(up_fail, int):
        return
    sd_ok, sd_fail = clips.get("sd_ok"), clips.get("sd_fail")
    ok = up_ok + (sd_ok if isinstance(sd_ok, int) else 0)
    fail = up_fail + (sd_fail if isinstance(sd_fail, int) else 0)
    if st.ok_val is None or ok < st.ok_val or fail < st.fail_base:
        st.ok_val, st.fail_base, st.ok_ts = ok, fail, now      # 첫 관측·카운터 리셋
        return
    if ok > st.ok_val:
        st.ok_val, st.fail_base, st.ok_ts = ok, fail, now
        _resolve_all(sb, st, camera_uuid, label, now)
        return
    stalled = now - st.ok_ts
    fails = fail - st.fail_base
    if stalled >= STALL_SEC and fails >= STALL_MIN_FAILS:
        _raise_alert(sb, st, camera_uuid, label, KIND_STALL, {
            "stalled_sec": int(stalled), "up_fail_delta": fails, "up_ok": up_ok,
            "up_fail": up_fail, "sd_ok": sd_ok, "sd_fail": sd_fail, "last_rec_s": clips.get("last_rec_s"),
            "last_err": clips.get("last_err"), "sd_backlog": clips.get("sd_backlog"),
        }, now)


def evaluate(sb: Any, camera_uuid: str, label: str, sys_state: dict[str, Any] | None,
             clips: dict[str, Any] | None, now: float | None = None) -> None:
    """카메라 heartbeat 1건 평가. 예외를 올리지 않는다."""
    try:
        _evaluate(sb, camera_uuid, label, sys_state, clips, time.time() if now is None else now)
    except Exception:  # noqa: BLE001 — 진단 알림은 보조 경로. heartbeat 를 막지 않는다
        logger.exception("camera_alerts 평가 실패 (camera=%s)", label)


__all__ = [
    "ABNORMAL_RESETS",
    "is_abnormal_reset",
    "KIND_RESET",
    "KIND_STALL",
    "STALL_MIN_FAILS",
    "STALL_SEC",
    "evaluate",
    "reset",
]
