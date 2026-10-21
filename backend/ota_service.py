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
# Terra Hub(Stage K): cameras 행이지만 펌웨어가 다른 프로젝트. model 로 target 을 가른다(2026-10-14).
HUB_MODEL = "esp32-p4-hub"
TARGET_HUB = "hub_p4"
# target 별 esp_app_desc.project_name(CMake project()). 카메라·허브가 같은 esp32p4 라 chip_id 로는
# 못 가르므로 release.project_name 과 교차 검증한다(스펙 J C6). None 이면(구 릴리스) 검사 생략.
EXPECTED_PROJECT_NAME: dict[str, str] = {
    "camera_p4": "firebeetle2_p4_yr030",
    "device_nano": "terra-iot",
    TARGET_HUB: "terra_hub_p4",
}


def target_for(kind: str, entity: dict[str, Any]) -> str:
    """이 기기가 받아야 할 release.target. 허브(cameras.model == esp32-p4-hub)만 hub_p4."""
    if kind not in TARGET_BY_KIND:
        raise OtaError(f"unknown kind {kind}", 400)
    if kind == KIND_CAMERA and entity.get("model") == HUB_MODEL:
        return TARGET_HUB
    return TARGET_BY_KIND[kind]

ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "accepted", "downloading", "ready", "applying"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"verified", "failed", "rolled_back", "timeout"})

# 사전 점검 게이트 임계 (리스크 저감 ③). 실패할 상황엔 애초에 보내지 않는다.
GATE_MIN_RSSI_DBM = -75          # 약한 WiFi 설치 판별선(앱 안내와 동일)
# 내부 RAM 최대 연속 블록. 다운로드는 ota_prepare 로 재부팅한 직후(내부 RAM 140KB+)에 하므로 평시 값은
# OTA 자체와 거의 무관 — 이 게이트는 "heartbeat 도 간신히 보내는 고갈 상태" 만 거른다.
# 2026-10-08: 베타 카메라 0.3.4 가 운영 중 15,360 을 정상으로 보고해 20KB 는 전부 막혔음 → 8KB.
GATE_MIN_INT_LARGEST_BYTES = 8 * 1024
# 부팅 직후 창. 원래 근거("센서/AE 수렴 중")는 OTA 와 무관하다 — prepare 는 어차피 재부팅해서
# 받는다. 실제로 막아야 하는 것은 **부팅 루프 중 OTA 시작** 하나뿐이다. 2026-10-16: 크래시가 잦은
# 보드에서 5분 창이 거의 열리지 않아(테스트마다 force) 재부팅 사유로 구분한다.
#   크래시(PANIC/WDT/INT_WDT/TASK_WDT/BROWNOUT) 직후 → 5분(부팅 루프 판별)
#   정상 재부팅(POWERON/SW:*/USB)                    → 60초(heartbeat·MQTT 안정화면 충분)
GATE_MIN_UPTIME_SEC = 300
GATE_MIN_UPTIME_CLEAN_SEC = 60
CRASH_RESETS: frozenset[str] = frozenset({"PANIC", "WDT", "INT_WDT", "TASK_WDT", "BROWNOUT"})
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

# [2026-10-21] 카메라 자동 재시도 (OtaMonitor). 재부팅 직후 P4↔C6 링크가 몇 분간 TCP 를 못 여는
# 보드(320062fb·19014459·441a8f8c)에서 prepare 다운로드가 `connect`/`read` 로 번번이 실패하고,
# 현장 조치가 불가한 베타 환경이라 원격으로는 "될 때까지 다시 보내기" 뿐이다(320062fb 는 4번째에
# 성공). 조건: 같은 (카메라, 릴리스) 로 24h 안 AUTO_RETRY_MAX 회까지, 직전 실패 뒤 INTERVAL 경과,
# 카메라 온라인·게이트 통과·라이브 없음·업로드 중 아님·최근 녹화 없음(재부팅 비용 최소화).
AUTO_RETRY_ERROR_PREFIXES: tuple[str, ...] = (
    "connect", "read", "read_short", "len_mismatch", "rejected_not_ready",
    "accepted > ", "downloading > ", "applying > ",
)
# [2026-10-21] apply 재전송. ready 뒤 서버가 ota_apply 를 보냈는데 기기 MQTT 가 그 순간 끊겨(clean
# session) 명령이 유실되면 기기는 다운로드한 슬롯을 든 채 그대로 돌고 서버는 applying 시한으로
# timeout 났다(320062fb 15:59, 441a8f8c 17:35). 기기 하트비트의 uptime 이 apply 시각 이후로도
# 계속 늘고 있으면(= 재부팅 안 함) 명령 유실이므로 같은 job_id 로 다시 보낸다. 펌웨어는
# NVS 의 ready 작업 id 와 비교해 맞으면 전환, 아니면 rejected_not_ready(→ 자동 재시도가 새 prepare).
APPLY_RESEND_AFTER_SEC = 60
APPLY_RESEND_MAX = 4
APPLY_RESEND_WINDOW_SEC = 1800     # timeout 으로 닫힌 applying 작업도 이 안이면 되살려 재전송
AUTO_RETRY_MAX = int(os.getenv("OTA_AUTO_RETRY_MAX", "6"))
AUTO_RETRY_INTERVAL_SEC = int(os.getenv("OTA_AUTO_RETRY_INTERVAL_SEC", "900"))
AUTO_RETRY_WINDOW_SEC = 24 * 3600
AUTO_RETRY_QUIET_REC_SEC = 600      # clip_stats.last_rec_s 가 이보다 작으면(10분 내 녹화) 보류


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
    reset = sys_.get("reset")
    # "SW:ota_apply(WDT)" 처럼 괄호로 하드웨어 사유가 덧붙는 형식도 크래시로 본다.
    crashed = isinstance(reset, str) and any(r in reset for r in CRASH_RESETS)
    min_uptime = GATE_MIN_UPTIME_SEC if crashed else GATE_MIN_UPTIME_CLEAN_SEC
    if isinstance(uptime, (int, float)) and uptime < min_uptime:
        reasons.append("just_booted")

    # 2026-10-16: "녹화·업로드·라이브 중" 은 더 이상 게이트가 아니다. 카메라 펌웨어 0.4.5 부터
    # ota_prepare 를 거절(ack busy)하지 않고 한가해질 때까지 최대 240초 미룬 뒤 재부팅한다
    # (펌웨어 app_ota.c OTA_DEFER_MAX_S). 서버가 먼저 409 로 막으면 운영자가 매번 force 를 눌러야
    # 하고, force 는 offline·weak_wifi·low_internal_ram 같은 쓸모 있는 게이트까지 함께 무력화한다.
    # 실측(951c1ba6, 10/16): 0.4.4 시도 8건 중 6건이 uploading 때문에 failed(busy).
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
    want = target_for(kind, entity)
    if release.get("target") != want:
        raise OtaError(
            f"release target {release.get('target')} 은 이 {kind}(기대 {want}) 용이 아님", 400
        )
    pn = release.get("project_name")
    exp_pn = EXPECTED_PROJECT_NAME.get(want)
    if pn and exp_pn and pn != exp_pn:
        raise OtaError(
            f"release project_name {pn!r} ≠ {want} 기대값 {exp_pn!r} — 다른 프로젝트 빌드", 400
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
        return _update(sb, job["id"], {"status": "verified", "pct": 100, "error": None})
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


def _auto_retry_error(err: str | None) -> bool:
    return bool(err) and any(str(err).startswith(p) for p in AUTO_RETRY_ERROR_PREFIXES)


def auto_retry_once(sb: Any, publish: Any, *, now: datetime | None = None) -> int:
    """실패(connect/read/시한)한 카메라 작업을 조건이 맞으면 같은 릴리스로 다시 발행. 발행 수 반환.

    `publish(camera_id_text, cmd) -> bool` 는 브리지의 publish_command. None 이면 아무것도 안 한다.
    """
    if publish is None:
        return 0
    now = now or _now()
    since = (now - timedelta(seconds=AUTO_RETRY_WINDOW_SEC)).isoformat()
    res = (
        sb.table("ota_jobs")
        .select("*")
        .eq("kind", KIND_CAMERA)
        .in_("status", ["failed", "timeout"])
        .gte("created_at", since)
        .order("created_at", desc=True)
        .execute()
    )
    retried = 0
    seen: set[str] = set()
    for job in res.data or []:
        tu = job["target_uuid"]
        if tu in seen:
            continue
        seen.add(tu)
        if not _auto_retry_error(job.get("error")):
            continue
        # 이 카메라의 가장 최근 작업이어야 한다(그 뒤에 사람이 보냈거나 성공했으면 건드리지 않음)
        latest = (
            sb.table("ota_jobs").select("id").eq("target_uuid", tu)
            .order("created_at", desc=True).limit(1).execute()
        )
        if not latest.data or latest.data[0]["id"] != job["id"]:
            continue
        ref = job.get("finished_at") or job.get("updated_at")
        try:
            if ref and (now - _parse_iso(ref)).total_seconds() < AUTO_RETRY_INTERVAL_SEC:
                continue
        except ValueError:
            continue
        tries = (
            sb.table("ota_jobs").select("id").eq("target_uuid", tu)
            .eq("release_id", job["release_id"]).gte("created_at", since).execute()
        )
        n_prev = len(tries.data or [])
        if n_prev >= AUTO_RETRY_MAX:
            continue
        cres = sb.table("cameras").select("*").eq("id", tu).limit(1).execute()
        cam = (cres.data or [None])[0]
        if not cam or cam.get("unlinked_at"):
            continue
        if cam.get("stream_mode") or cam.get("live_session_id"):
            continue
        cs = cam.get("clip_stats") if isinstance(cam.get("clip_stats"), dict) else {}
        if isinstance(cs.get("up_busy_s"), (int, float)) and cs["up_busy_s"] >= 0:
            continue
        last_rec = cs.get("last_rec_s")
        if isinstance(last_rec, (int, float)) and 0 <= last_rec < AUTO_RETRY_QUIET_REC_SEC:
            continue
        if gate_reasons(KIND_CAMERA, cam, now=now):
            continue
        try:
            new_job, release, payload = create_job(
                sb, kind=KIND_CAMERA, entity=cam, release_id=job["release_id"],
                issued_by=job.get("issued_by"),
            )
        except OtaError as exc:
            logger.info("ota auto-retry 건너뜀 camera=%s: %s", cam.get("camera_id"), exc.detail)
            continue
        from backend.mqtt.camera_commands import ota_prepare_command  # 지연 import(순환 방지)
        cmd = ota_prepare_command(
            job_id=payload["job_id"], version=payload["version"], size_bytes=payload["size"],
            sha256=payload["sha256"], url=payload["url"],
        )
        set_prepare_msg(sb, new_job["id"], cmd["msg_id"])
        ok = False
        try:
            ok = bool(publish(cam.get("camera_id", ""), cmd))
        except Exception:  # noqa: BLE001
            logger.exception("ota auto-retry 발행 예외 camera=%s", cam.get("camera_id"))
        if not ok:
            mark_failed(sb, new_job["id"], "publish_failed")
            continue
        logger.warning("ota auto-retry %d/%d camera=%s job=%s (이전 %s: %s) version=%s",
                       n_prev + 1, AUTO_RETRY_MAX, cam.get("camera_id"), new_job["id"],
                       job["status"], job.get("error"), release.get("version"))
        retried += 1
    return retried


def _alive_without_reboot_since(cam: dict[str, Any], since_iso: str, now: datetime) -> bool:
    """하트비트가 최근이고 uptime 이 since 이후 경과 시간보다 크면 = 그 뒤 재부팅 없이 살아 있음."""
    last_seen = cam.get("last_seen_at")
    uptime = _sys_of(KIND_CAMERA, cam).get("uptime_s")
    if not last_seen or not isinstance(uptime, (int, float)):
        return False
    try:
        if (now - _parse_iso(last_seen)).total_seconds() > GATE_ONLINE_WITHIN_SEC:
            return False
        return uptime > (now - _parse_iso(since_iso)).total_seconds() + 10
    except ValueError:
        return False


def _resent_count(job: dict[str, Any]) -> int:
    err = str(job.get("error") or "")
    if err.startswith("apply_resent "):
        try:
            return int(err.split()[1])
        except (IndexError, ValueError):
            return 0
    return 0


def resend_apply_once(sb: Any, publish: Any, *, now: datetime | None = None) -> int:
    """유실된 ota_apply 재전송. 대상: applying 인데 apply 뒤 APPLY_RESEND_AFTER_SEC 지나도록 기기가
    재부팅하지 않은 작업, 그리고 그 상태로 timeout 된 지 APPLY_RESEND_WINDOW_SEC 안인 작업. 발행 수 반환."""
    if publish is None:
        return 0
    now = now or _now()
    since = (now - timedelta(seconds=APPLY_RESEND_WINDOW_SEC)).isoformat()
    res = (
        sb.table("ota_jobs").select("*").eq("kind", KIND_CAMERA)
        .in_("status", ["applying", "timeout"]).gte("created_at", since)
        .order("created_at", desc=True).execute()
    )
    sent = 0
    seen: set[str] = set()
    for job in res.data or []:
        tu = job["target_uuid"]
        if tu in seen:
            continue
        seen.add(tu)
        applied_at = job.get("applied_at")
        if not applied_at:
            continue
        if job["status"] == "timeout" and not str(job.get("error") or "").startswith("applying > "):
            continue
        if _resent_count(job) >= APPLY_RESEND_MAX:
            continue
        try:
            age = (now - _parse_iso(applied_at)).total_seconds()
        except ValueError:
            continue
        if age < APPLY_RESEND_AFTER_SEC:
            continue
        latest = (
            sb.table("ota_jobs").select("id").eq("target_uuid", tu)
            .order("created_at", desc=True).limit(1).execute()
        )
        if not latest.data or latest.data[0]["id"] != job["id"]:
            continue
        cres = sb.table("cameras").select("*").eq("id", tu).limit(1).execute()
        cam = (cres.data or [None])[0]
        if not cam or cam.get("unlinked_at"):
            continue
        if job.get("prev_version") and cam.get("firmware_ver") != job["prev_version"]:
            continue                                  # 이미 바뀌었으면 heartbeat 판정에 맡긴다
        if not _alive_without_reboot_since(cam, applied_at, now):
            continue
        from backend.mqtt.camera_commands import ota_apply_command  # 지연 import(순환 방지)
        cmd = ota_apply_command(job_id=job["id"])
        ok = False
        try:
            ok = bool(publish(cam.get("camera_id", ""), cmd))
        except Exception:  # noqa: BLE001
            logger.exception("ota apply 재전송 예외 camera=%s", cam.get("camera_id"))
        if not ok:
            continue
        n = _resent_count(job) + 1
        _update(sb, job["id"], {"status": "applying", "apply_msg_id": cmd["msg_id"],
                                "applied_at": _now_iso(), "finished_at": None,
                                "error": f"apply_resent {n}"})
        logger.warning("ota apply 재전송 %d/%d camera=%s job=%s (uptime %s, 이전 상태 %s)",
                       n, APPLY_RESEND_MAX, cam.get("camera_id"), job["id"],
                       _sys_of(KIND_CAMERA, cam).get("uptime_s"), job["status"])
        sent += 1
    return sent


class OtaMonitor:
    """브리지 프로세스 스레드. 60초마다 scan_once + 카메라 자동 재시도 + apply 재전송."""

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
            try:
                from backend.mqtt import handlers as _h  # 브리지가 등록한 publish_command
                from backend.supabase_client import get_supabase_client
                sb_ = get_supabase_client()
                pub = _h.get_command_publisher()
                m = resend_apply_once(sb_, pub)
                if m:
                    logger.info("ota apply 재전송 %d건", m)
                n = auto_retry_once(sb_, pub)
                if n:
                    logger.info("ota auto-retry 발행 %d건", n)
            except Exception:  # noqa: BLE001
                logger.exception("ota auto-retry 실패")
            self._stop.wait(self._interval)


__all__ = [
    "ACTIVE_STATUSES",
    "KIND_CAMERA",
    "KIND_DEVICE",
    "OtaError",
    "OtaMonitor",
    "EXPECTED_PROJECT_NAME",
    "HUB_MODEL",
    "TARGET_BY_KIND",
    "TARGET_HUB",
    "TERMINAL_STATUSES",
    "target_for",
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
    "auto_retry_once",
    "resend_apply_once",
    "mark_failed",
    "scan_once",
    "set_prepare_msg",
]
