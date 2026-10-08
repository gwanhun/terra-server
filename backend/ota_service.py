"""
펌웨어 OTA 작업 서비스 — Stage J (specs/stage-j-ota.md).

세 곳이 공유하는 로직을 한 곳에 둔다:
- 라우터(cameras / commands): 작업 생성 + 사전 점검 게이트 + 명령 발행 + apply 발행
- MQTT 핸들러(handlers): ack 의 `ota` 블록 → 상태 전이, heartbeat `fw` → verified / rolled_back
- OtaMonitor(브리지 스레드): 단계별 시한 초과 → timeout, 기기 commands 실패 → failed

## 상태 전이

    pending ─(ack ok)→ accepted ─(ota pct)→ downloading ─(ota ready)→ ready
       │                  │                     │                      │
       └──────────────────┴─────────────────────┴──(ota failed / 거부 / 시한)──→ failed | timeout
    ready ─(POST .../apply)→ applying ─(heartbeat fw == version)→ verified
                                 └─(heartbeat fw == prev_version, uptime 리셋)→ rolled_back
                                 └─(시한)→ timeout

## 왜 2단계?
prepare 는 부팅 파티션을 바꾸지 않아 실패해도 아무 일이 없다. 18대 전체에 prepare 만 먼저 보내
다운로드 경로를 무위험으로 검증하고, apply 는 운영자가 조용한 시간대에 보낸다(리스크 ①).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.mqtt.camera_commands import ota_prepare_payload

logger = logging.getLogger(__name__)

# ---------- 상수 ----------

KIND_CAMERA = "camera"
KIND_DEVICE = "device"
# release.target ↔ job.kind. supermini 는 범위 밖(owner 결정 2026-10-06).
TARGET_BY_KIND: dict[str, str] = {KIND_CAMERA: "camera_p4", KIND_DEVICE: "device_nano"}

ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "accepted", "downloading", "ready", "applying"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"verified", "failed", "rolled_back", "timeout"})

# 사전 점검 게이트 임계 (리스크 저감 ③). 실패할 상황엔 애초에 보내지 않는다.
GATE_MIN_RSSI_DBM = -75          # 약한 WiFi 설치 판별선(앱 안내와 동일)
# 내부 RAM 최대 연속 블록. 다운로드는 ota_prepare 로 재부팅한 직후(내부 RAM 140KB+)에 하므로 평시 값은
# OTA 자체와 거의 무관 — 이 게이트는 "heartbeat 도 간신히 보내는 고갈 상태" 만 거른다.
# 2026-10-08: 베타 카메라 0.3.4 가 운영 중 15,360 을 정상으로 보고해 20KB 는 전부 막혔음 → 8KB.
GATE_MIN_INT_LARGEST_BYTES = 8 * 1024
GATE_MIN_UPTIME_SEC = 300        # 부팅 직후 5분은 센서/AE 수렴 중
GATE_ONLINE_WITHIN_SEC = 90      # last_seen 이 이보다 오래되면 오프라인 취급

# 단계별 시한 (OtaMonitor). ready 는 운영자 대기라 시한 없음.
TIMEOUT_BY_STATUS_SEC: dict[str, int] = {
    "pending": 180,         # 발행 후 3분 안에 ack 없음 → 명령 유실(TTL 60초) 또는 구 펌웨어 무응답
    "accepted": 600,        # 예약 뒤 재부팅·다운로드 시작까지
    "downloading": 900,     # 진행률 갱신이 15분 멈춤
    "applying": 600,        # 전환 재부팅 후 10분 안에 heartbeat 없음
}

# 펌웨어가 보고하는 실패 사유 중 "다시 보내면 될 수도" 인 것 (콘솔 안내용)
RETRYABLE_ERRORS: frozenset[str] = frozenset({"busy", "dns", "tcp", "tls", "http", "timeout"})


class OtaError(ValueError):
    """작업 생성/전이 거부 — 라우터에서 4xx 로 변환. `status_code` 힌트 포함."""

    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def api_public_base_url() -> str:
    """펌웨어가 바이너리를 받으러 올 공개 주소. 끝 슬래시 없음."""
    base = os.getenv("API_PUBLIC_BASE_URL", "https://api.terra-server.uk").strip()
    return base.rstrip("/")


def download_url(job_id: str) -> str:
    return f"{api_public_base_url()}/firmware/jobs/{job_id}/bin"


# ---------- 조회 ----------

def get_release(sb: Any, release_id: str) -> dict[str, Any] | None:
    res = sb.table("firmware_releases").select("*").eq("id", release_id).limit(1).execute()
    return (res.data or [None])[0]


def get_job(sb: Any, job_id: str) -> dict[str, Any] | None:
    res = sb.table("ota_jobs").select("*").eq("id", job_id).limit(1).execute()
    return (res.data or [None])[0]


def active_job_for(sb: Any, target_uuid: str) -> dict[str, Any] | None:
    res = (
        sb.table("ota_jobs")
        .select("*")
        .eq("target_uuid", target_uuid)
        .in_("status", sorted(ACTIVE_STATUSES))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def _sys_of(kind: str, entity: dict[str, Any]) -> dict[str, Any]:
    """카메라는 clip_stats.sys, 기기는 sys_state 에 uptime/rssi/int_largest 가 있다."""
    if kind == KIND_CAMERA:
        cs = entity.get("clip_stats")
        sys_ = cs.get("sys") if isinstance(cs, dict) else None
    else:
        sys_ = entity.get("sys_state")
    return sys_ if isinstance(sys_, dict) else {}


# ---------- 사전 점검 게이트 ----------

def gate_reasons(kind: str, entity: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    """보내면 안 되는 이유 목록(빈 리스트 = 통과). 라우터는 `force=True` 면 무시하고 기록만 한다."""
    now = now or _now()
    reasons: list[str] = []

    last_seen = entity.get("last_seen_at")
    if not last_seen:
        reasons.append("never_seen")
    else:
        try:
            age = (now - _parse_iso(last_seen)).total_seconds()
        except ValueError:
            age = float("inf")
        if age > GATE_ONLINE_WITHIN_SEC or not entity.get("is_online", False):
            reasons.append("offline")

    caps = entity.get("capabilities")
    if not (isinstance(caps, dict) and caps.get("ota")):
        reasons.append("no_ota_capability")   # OTA 지원 펌웨어는 heartbeat capabilities.ota=true 보고

    sys_ = _sys_of(kind, entity)
    rssi = sys_.get("rssi")
    if isinstance(rssi, (int, float)) and rssi < GATE_MIN_RSSI_DBM:
        reasons.append("weak_wifi")
    largest = sys_.get("int_largest")
    if isinstance(largest, (int, float)) and largest < GATE_MIN_INT_LARGEST_BYTES:
        reasons.append("low_internal_ram")
    uptime = sys_.get("uptime_s")
    if isinstance(uptime, (int, float)) and uptime < GATE_MIN_UPTIME_SEC:
        reasons.append("just_booted")

    if kind == KIND_CAMERA:
        clips = entity.get("clip_stats")
        if isinstance(clips, dict):
            busy = clips.get("up_busy_s")
            if isinstance(busy, (int, float)) and busy >= 0:
                reasons.append("uploading")
            last_err = clips.get("last_err")
            # SD fault 는 heartbeat 에 직접 안 실린다 — sd_fail 급증은 _warn_clip_regression 몫.
            _ = last_err
        # 라이브 중 = cameras.live_session_id 가 있음 (backend/live_session.py 가 종료 시 NULL 로 지움).
        if entity.get("live_session_id"):
            reasons.append("live_active")
    return reasons


# ---------- 작업 생성 ----------

def create_job(
    sb: Any,
    *,
    kind: str,
    entity: dict[str, Any],
    release_id: str,
    issued_by: str | None,
    force: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """게이트 통과 → ota_jobs INSERT. (job, release, prepare_payload) 반환.

    발행은 호출자가 한다(카메라: 직접 publish, 기기: commands INSERT). 발행 실패 시
    `mark_failed(job, "publish_failed")` 로 되돌린다.
    """
    if kind not in TARGET_BY_KIND:
        raise OtaError(f"unknown kind {kind}", 400)
    release = get_release(sb, release_id)
    if not release:
        raise OtaError("release not found", 404)
    if release.get("target") != TARGET_BY_KIND[kind]:
        raise OtaError(
            f"release target {release.get('target')} 은 {kind} 용이 아님", 400
        )
    if release.get("retired_at"):
        raise OtaError(
            "release retired: " + str(release.get("retired_reason") or release.get("version")), 409
        )
    if entity.get("firmware_ver") == release.get("version"):
        raise OtaError("already on this version", 409)

    active = active_job_for(sb, entity["id"])
    if active:
        raise OtaError(f"active job exists: {active['id']} ({active['status']})", 409)

    reasons = gate_reasons(kind, entity)
    if reasons and not force:
        raise OtaError("precheck failed: " + ",".join(reasons), 409)
    if reasons:
        logger.warning("ota force: %s %s 게이트 무시 %s", kind, entity.get("id"), reasons)

    row = {
        "kind": kind,
        "target_uuid": entity["id"],
        "release_id": release["id"],
        "status": "pending",
        "prev_version": entity.get("firmware_ver"),
        "issued_by": issued_by,
        "forced": bool(reasons),
    }
    res = sb.table("ota_jobs").insert(row).execute()
    job = (res.data or [None])[0]
    if not job:
        raise OtaError("ota_jobs INSERT 실패", 500)

    payload = ota_prepare_payload(
        job_id=job["id"],
        version=release["version"],
        size_bytes=int(release["size_bytes"]),
        sha256=release["sha256"],
        url=download_url(job["id"]),
    )
    return job, release, payload


# ---------- 상태 전이 ----------

def _update(sb: Any, job_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    patch = {**patch, "updated_at": _now_iso()}
    if patch.get("status") in TERMINAL_STATUSES:
        patch.setdefault("finished_at", _now_iso())
    res = sb.table("ota_jobs").update(patch).eq("id", job_id).execute()
    return (res.data or [None])[0]


def set_prepare_msg(sb: Any, job_id: str, msg_id: str) -> None:
    _update(sb, job_id, {"prepare_msg_id": msg_id})


def mark_failed(sb: Any, job_id: str, error: str) -> None:
    _update(sb, job_id, {"status": "failed", "error": error[:200]})


def mark_downloading(sb: Any, job_id: str, pct: int = 0) -> None:
    """바이너리 엔드포인트가 첫 바이트를 보내기 전에 호출. 펌웨어 진행률 ack 가 이후 덮어쓴다."""
    _update(sb, job_id, {"status": "downloading", "pct": max(0, min(100, int(pct)))})


def mark_applying(sb: Any, job_id: str, msg_id: str) -> None:
    _update(sb, job_id, {"status": "applying", "apply_msg_id": msg_id, "applied_at": _now_iso()})


def _find_job_by_msg(sb: Any, msg_id: str) -> dict[str, Any] | None:
    for col in ("prepare_msg_id", "apply_msg_id"):
        res = sb.table("ota_jobs").select("*").eq(col, msg_id).limit(1).execute()
        row = (res.data or [None])[0]
        if row:
            return row
    return None


def apply_ack(sb: Any, msg_id: str, result: str | None, ota: dict[str, Any] | None) -> dict[str, Any] | None:
    """ack 1건을 작업 상태에 반영. 작업과 무관한 ack 면 None.

    펌웨어 ack 계약(docs/MQTT.md §3-b):
      첫 ack            {msg_id, result:"ok"|"busy"|"rejected_unknown_action", state:"OTA_PREPARE"|"OTA_APPLY"}
      진행/결과 ack     {msg_id, result:"ok"|"error", ota:{job_id, phase:"downloading"|"ready"|"failed", pct?, error?}}
    """
    job = _find_job_by_msg(sb, msg_id)
    if not job:
        return None
    if job["status"] in TERMINAL_STATUSES:
        logger.info("ota ack 무시: job %s 는 이미 %s", job["id"], job["status"])
        return job

    phase = ota.get("phase") if isinstance(ota, dict) else None
    if phase:
        if ota.get("job_id") and ota.get("job_id") != job["id"]:
            logger.warning("ota ack job_id 불일치: msg=%s ack=%s job=%s", msg_id, ota.get("job_id"), job["id"])
            return job
        if phase == "downloading":
            pct = ota.get("pct")
            patch: dict[str, Any] = {"status": "downloading"}
            if isinstance(pct, (int, float)):
                patch["pct"] = max(0, min(100, int(pct)))
            return _update(sb, job["id"], patch)
        if phase == "ready":
            return _update(sb, job["id"], {"status": "ready", "pct": 100})
        if phase == "failed":
            err = str(ota.get("error") or result or "firmware_error")
            return _update(sb, job["id"], {"status": "failed", "error": err[:200]})
        logger.warning("ota ack 알 수 없는 phase=%s (job %s)", phase, job["id"])
        return job

    # 첫 ack (prepare / apply 수락 여부)
    if result == "ok":
        if job["status"] == "pending":
            return _update(sb, job["id"], {"status": "accepted"})
        return job   # applying 의 ok ack: 재부팅 직전. heartbeat 가 종결 짓는다.
    if result in ("busy", "rejected_busy"):
        return _update(sb, job["id"], {"status": "failed", "error": "busy"})
    if result in ("rejected_unknown_action", "unknown_action"):
        return _update(sb, job["id"], {"status": "failed", "error": "old_firmware"})
    if result and result != "ok":
        return _update(sb, job["id"], {"status": "failed", "error": str(result)[:200]})
    return job


def apply_heartbeat_fw(sb: Any, target_uuid: str, fw: str) -> dict[str, Any] | None:
    """applying 중인 작업에 heartbeat `fw` 를 대조해 verified / rolled_back 을 판정."""
    res = (
        sb.table("ota_jobs")
        .select("id, status, release_id, prev_version")
        .eq("target_uuid", target_uuid)
        .eq("status", "applying")
        .limit(1)
        .execute()
    )
    job = (res.data or [None])[0]
    if not job:
        return None
    release = get_release(sb, job["release_id"])
    version = release.get("version") if release else None
    if version and fw == version:
        logger.info("ota verified: job %s fw=%s", job["id"], fw)
        return _update(sb, job["id"], {"status": "verified", "pct": 100})
    if job.get("prev_version") and fw == job["prev_version"]:
        logger.warning("ota rolled back: job %s fw=%s (expected %s)", job["id"], fw, version)
        return _update(sb, job["id"], {"status": "rolled_back", "error": "bootloader_rollback"})
    logger.warning("ota applying 중 예상 밖 fw=%s (job %s, expected %s, prev %s)",
                   fw, job["id"], version, job.get("prev_version"))
    return job


# ---------- 시한 스윕 ----------

def scan_once(sb: Any | None = None) -> dict[str, int]:
    """진행 중 작업의 시한 초과 → timeout. 기기 작업은 commands 실패도 failed 로 굳힌다."""
    if sb is None:
        from backend.supabase_client import get_supabase_client
        sb = get_supabase_client()
    now = _now()
    res = (
        sb.table("ota_jobs")
        .select("id, kind, status, updated_at, applied_at, prepare_msg_id, apply_msg_id")
        .in_("status", sorted(ACTIVE_STATUSES))
        .execute()
    )
    timed_out = failed = 0
    for job in res.data or []:
        status = job["status"]
        # 기기: dispatcher 가 commands 를 expired/rejected/no_ack 로 굳히면 작업도 실패
        if job["kind"] == KIND_DEVICE and status in ("pending", "applying"):
            cmd_id = job.get("apply_msg_id") if status == "applying" else job.get("prepare_msg_id")
            if cmd_id:
                cres = sb.table("commands").select("status, result").eq("id", cmd_id).limit(1).execute()
                cmd = (cres.data or [None])[0]
                if cmd and cmd.get("status") in ("expired", "rejected", "failed"):
                    reason = cmd.get("result") or cmd.get("status")
                    if reason == "unknown_action":
                        reason = "old_firmware"
                    _update(sb, job["id"], {"status": "failed", "error": str(reason)[:200]})
                    failed += 1
                    continue
        limit = TIMEOUT_BY_STATUS_SEC.get(status)
        if not limit:
            continue
        ref = job.get("applied_at") if status == "applying" else job.get("updated_at")
        if not ref:
            continue
        try:
            age = (now - _parse_iso(ref)).total_seconds()
        except ValueError:
            continue
        if age > limit:
            _update(sb, job["id"], {"status": "timeout", "error": f"{status} > {limit}s"})
            logger.warning("ota timeout: job %s (%s, %.0fs)", job["id"], status, age)
            timed_out += 1
    return {"timeout": timed_out, "failed": failed}


class OtaMonitor:
    """브리지 프로세스 스레드. 60초마다 scan_once (OfflineMonitor 와 같은 모양)."""

    def __init__(self, interval_sec: float = 60.0) -> None:
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="ota-monitor")
        self._thread.start()
        logger.info("ota monitor 시작 (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                stats = scan_once()
                if stats["timeout"] or stats["failed"]:
                    logger.info("ota scan: %s", stats)
            except Exception:  # noqa: BLE001
                logger.exception("ota scan 실패")
            self._stop.wait(self._interval)


__all__ = [
    "ACTIVE_STATUSES",
    "KIND_CAMERA",
    "KIND_DEVICE",
    "OtaError",
    "OtaMonitor",
    "TARGET_BY_KIND",
    "TERMINAL_STATUSES",
    "TIMEOUT_BY_STATUS_SEC",
    "active_job_for",
    "apply_ack",
    "apply_heartbeat_fw",
    "create_job",
    "download_url",
    "gate_reasons",
    "get_job",
    "get_release",
    "mark_applying",
    "mark_failed",
    "scan_once",
    "set_prepare_msg",
]
