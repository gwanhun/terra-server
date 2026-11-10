"""
오프라인 감시 — Stage D.

매 1분 주기로 last_seen_at 검사 → 3분 이상 무응답이면:
- devices: is_online = false + alerts INSERT (kind='offline')
- cameras: is_online = false 만 (카메라용 alert 파이프라인 없음)
재연결 시 handle_telemetry/handle_ack 가 is_online=true 로 자동 복원하고,
본 모니터가 다음 주기에 offline alert 를 resolve.

## 교차 계정 superseded (Stage U, 2026-11-10) — `scan_supersede_once`, 10분 주기

같은 보드(hw_id)를 다른 계정이 등록하면 이전 계정 행이 활성으로 남는다. 페어링 요청 안에서
이전 행을 건드리면 안 된다 — hw_id 는 검증 없는 본문 값이라 남의 MAC 으로 페어링해 그 사람
카메라를 끊을 수 있고, 페어링 응답 토큰으로 브로커에 붙어 텔레메트리도 위조할 수 있다.
공격자가 위조할 수 없는 신호는 "피해자 보드가 꺼졌다" 뿐이다. 그래서:
- 판정: 새 행(다른 계정) 온라인 **그리고** 이전 행 24시간 이상 오프라인 → `superseded_at` (목록 숨김만)
- 복구: superseded 행이 다시 온라인(옛 자격증명으로 접속) → 해제 + 새 행에 `supersede_suspect_at`
- 해지: superseded 30일 뒤 재접속 없음 → Mosquitto 계정 해지(`mqtt_revoked_at`)
정전·여행으로 정상 보드가 하루 꺼져도 숨김만 되고 돌아오면 자동 복구된다. 스펙: specs/stage-u-camera-rebind-supersede.md

> cameras 를 빼먹으면 카메라가 한 번 online 된 뒤 영원히 online 으로 표시된다(웹 거짓 표시).

## 임계값

| 임계 | 의미 |
|------|------|
| 1분  | 정상 변동 범위 (3초 주기 telemetry × 20 마진) |
| 3분  | **offline 판정** (네트워크 일시 끊김 vs 진짜 다운 구분) |
| 10분 | critical (장기 장애, severity 상향) |

## 단일 인스턴스 가정

terra-bridge.service 가 1개 → 본 모니터도 1개.
멀티 인스턴스면 같은 alert 를 여러 번 INSERT 할 수 있음 (alerts.py 의 dedup 로 어느정도 막힘).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.mqtt import registry
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60.0
OFFLINE_THRESHOLD_SEC = 180  # 3분 (디바이스: telemetry 3초 주기 기준)
CRITICAL_THRESHOLD_SEC = 600  # 10분
# 카메라 telemetry 는 15초 주기(MQTT_TELEMETRY_PERIOD_MS) → 180초면 12 비트 연속 유실 = 사실상
# 끊긴 것(QoS0 라도 12 연속 유실은 끊김 외엔 드묾). 디바이스와 동일 임계값. 너무 길면(10분 등)
# 끊긴 카메라가 한참 online 으로 남아 표시가 굼떠 보인다 → 3분으로 응답성 확보.
# 라이브 스트리밍 중 telemetry 가 밀려 깜빡이면 그때 상향 검토.
CAMERA_OFFLINE_THRESHOLD_SEC = 180  # 3분

# Stage U — 교차 계정 superseded. 24시간은 "정전·여행으로 정상 보드가 하루 꺼질 수 있다" 와
# "양도 뒤 이전 계정 목록에 유령 카메라가 하루쯤 남는 건 참을 수 있다" 의 절충(2026-11-10 결정).
SUPERSEDE_INTERVAL_SEC = 600            # 스캔 주기 10분 — 24시간 판정에 분 단위 정밀도는 불필요
SUPERSEDE_OFFLINE_SEC = 24 * 3600       # 이전 행이 이만큼 오프라인이면 superseded
SUPERSEDE_REVOKE_SEC = 30 * 24 * 3600   # superseded 뒤 이만큼 재접속 없으면 MQTT 계정 해지


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_offline_alert(sb, device_uuid: str, age_sec: float) -> None:
    """이미 활성 offline alert 있으면 스킵."""
    res = (
        sb.table("alerts")
        .select("id, severity")
        .eq("device_id", device_uuid)
        .eq("kind", "offline")
        .is_("resolved_at", "null")
        .limit(1)
        .execute()
    )
    active = (res.data or [None])[0]

    severity = "critical" if age_sec > CRITICAL_THRESHOLD_SEC else "warning"

    if active:
        # 이미 활성 → severity 상향 가능 (warning → critical)
        if active["severity"] != severity and severity == "critical":
            try:
                sb.table("alerts").update({"severity": severity}).eq(
                    "id", active["id"]
                ).execute()
                logger.info("offline alert severity 상향: device=%s → critical", device_uuid)
            except Exception:  # noqa: BLE001
                logger.exception("offline severity 업데이트 실패")
        return

    try:
        sb.table("alerts").insert({
            "device_id": device_uuid,
            "kind": "offline",
            "severity": severity,
            "message": f"디바이스 {int(age_sec)}초 무응답",
            "context": {"last_seen_age_sec": int(age_sec)},
        }).execute()
        logger.warning("offline alert: device=%s age=%ds", device_uuid, int(age_sec))
    except Exception:  # noqa: BLE001
        logger.exception("offline alert INSERT 실패: device=%s", device_uuid)


def _resolve_offline_alert(sb, device_uuid: str) -> None:
    res = (
        sb.table("alerts")
        .select("id")
        .eq("device_id", device_uuid)
        .eq("kind", "offline")
        .is_("resolved_at", "null")
        .limit(1)
        .execute()
    )
    active = (res.data or [None])[0]
    if not active:
        return
    try:
        sb.table("alerts").update({
            "resolved_at": _now().isoformat(),
        }).eq("id", active["id"]).execute()
        logger.info("offline alert RESOLVED: device=%s", device_uuid)
    except Exception:  # noqa: BLE001
        logger.exception("offline resolve 실패: device=%s", device_uuid)


def _age_sec(now: datetime, ts_raw: Any) -> float | None:
    """ISO 문자열 → 경과 초. 없거나 파싱 실패면 None(판정 보류)."""
    if not ts_raw:
        return None
    try:
        return (now - _parse_iso(str(ts_raw))).total_seconds()
    except ValueError:
        return None


_SUPERSEDE_COLUMNS = (
    "id, owner_id, hw_id, camera_id, is_online, last_seen_at, created_at, "
    "superseded_at, superseded_by, mqtt_revoked_at"
)


def scan_supersede_once(sb: Any | None = None, now: datetime | None = None) -> dict[str, int]:
    """교차 계정 superseded 판정·복구·해지 1회 (모듈 docstring 참고). 통계 반환.

    대상은 활성(unlinked_at IS NULL)·hw_id 있는 cameras 전부(수백 행) — 한 번 읽어 Python 에서
    hw_id 로 묶는다. 구 펌웨어(hw_id NULL)는 보드를 알 수 없어 제외.
    순서가 중요하다: 복구 → 판정 → 해지. 복구된 행은 같은 스캔의 판정·해지 대상에서 빠진다.
    """
    sb = sb or get_supabase_client()
    now = now or _now()
    res = (
        sb.table("cameras")
        .select(_SUPERSEDE_COLUMNS)
        .is_("unlinked_at", "null")
        .not_.is_("hw_id", "null")
        .execute()
    )
    rows: list[dict[str, Any]] = list(res.data or [])
    stats = {"superseded": 0, "recovered": 0, "revoked": 0, "conflict": 0}
    now_iso = now.isoformat()

    # 1) 복구 — 숨긴 행의 보드가 옛 자격증명으로 다시 접속했다(브리지 _lookup_active_uuid 는
    #    unlinked_at 만 보므로 heartbeat 가 그대로 들어와 is_online=true 가 된다). 공격자는 피해자
    #    보드를 켤 수 없으므로 이 신호는 위조 불가 → 밀어낸 새 행이 의심스럽다.
    for r in rows:
        if not (r.get("superseded_at") and r.get("is_online")):
            continue
        sb.table("cameras").update({"superseded_at": None, "superseded_by": None}).eq("id", r["id"]).execute()
        if r.get("superseded_by"):
            sb.table("cameras").update({"supersede_suspect_at": now_iso}).eq("id", r["superseded_by"]).execute()
        logger.warning(
            "supersede 복구: camera=%s(hw_id=%s) 가 다시 온라인 — 밀어낸 행 %s 에 의심 표식",
            r["camera_id"], r.get("hw_id"), r.get("superseded_by"),
        )
        r["superseded_at"] = None
        r["superseded_by"] = None
        stats["recovered"] += 1

    # 2) 판정 — hw_id 묶음에 소유자 2명 이상, 온라인 소유자 1명(승자). 다른 소유자 행이 24시간
    #    이상 오프라인(한 번도 안 붙은 행은 생성 24시간 경과)이면 superseded.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if not r.get("superseded_at"):
            groups[str(r["hw_id"])].append(r)
    for hw_id, grp in groups.items():
        if len({r["owner_id"] for r in grp}) < 2:
            continue
        online = [r for r in grp if r.get("is_online")]
        if not online:
            continue
        if len({r["owner_id"] for r in online}) > 1:
            # 한 보드가 두 계정으로 동시에 붙어 있을 수는 없다 → 위조 의심. 판정 보류.
            stats["conflict"] += 1
            logger.warning("supersede 보류: hw_id=%s 소유자 2명 이상 동시 온라인 %s",
                           hw_id, [r["camera_id"] for r in online])
            continue
        winner = max(online, key=lambda r: str(r.get("last_seen_at") or ""))
        for r in grp:
            if r["owner_id"] == winner["owner_id"]:
                continue
            age = _age_sec(now, r.get("last_seen_at") or r.get("created_at"))
            if age is None or age <= SUPERSEDE_OFFLINE_SEC:
                continue
            try:
                sb.table("cameras").update(
                    {"superseded_at": now_iso, "superseded_by": winner["id"]}
                ).eq("id", r["id"]).execute()
            except Exception:  # noqa: BLE001
                logger.exception("superseded UPDATE 실패 camera=%s", r["camera_id"])
                continue
            r["superseded_at"] = now_iso
            stats["superseded"] += 1
            logger.info("superseded: camera=%s(owner=%s) ← 새 행 %s(owner=%s), hw_id=%s, 오프라인 %dh",
                        r["camera_id"], r["owner_id"], winner["camera_id"], winner["owner_id"],
                        hw_id, int(age // 3600))

    # 3) 해지 — superseded 30일, 재접속 없음. 늦게 하는 이유: 정상 보드가 돌아올 길을 남긴다.
    for r in rows:
        if not r.get("superseded_at") or r.get("mqtt_revoked_at"):
            continue
        age = _age_sec(now, r["superseded_at"])
        if age is None or age <= SUPERSEDE_REVOKE_SEC:
            continue
        ok = registry.unregister_device(r["camera_id"])
        if not ok:
            logger.error("supersede 해지 실패(헬퍼) camera=%s — 다음 스캔에 재시도", r["camera_id"])
            continue
        sb.table("cameras").update({"mqtt_revoked_at": now_iso}).eq("id", r["id"]).execute()
        stats["revoked"] += 1
        logger.warning("supersede MQTT 해지: camera=%s hw_id=%s (superseded %s)",
                       r["camera_id"], r.get("hw_id"), r["superseded_at"])

    return stats


def _scan_cameras_offline(sb, now: datetime) -> int:
    """카메라 is_online 갱신 (offline 판정만).

    카메라는 bridge 가 telemetry/ack 로 is_online=True 만 세팅하고, 어디서도 False 로
    되돌리지 않아 한 번 online 되면 영원히 online 으로 박힌다(웹 표시가 거짓말). devices 와 달리
    카메라용 alert 파이프라인은 없으므로 여기선 **is_online=False 플립만** 한다.
    재연결 시 handle_telemetry/handle_ack 가 다시 True 로 복원한다.
    """
    res = (
        sb.table("cameras")
        .select("id, last_seen_at, is_online")
        .is_("unlinked_at", "null")
        .execute()
    )
    flipped = 0
    for row in res.data or []:
        last_seen_raw = row.get("last_seen_at")
        if not last_seen_raw or not bool(row.get("is_online")):
            continue  # 한 번도 안 켜졌거나 이미 offline
        age = (now - _parse_iso(last_seen_raw)).total_seconds()
        if age > CAMERA_OFFLINE_THRESHOLD_SEC:
            try:
                sb.table("cameras").update({"is_online": False}).eq("id", row["id"]).execute()
                flipped += 1
                logger.info("camera offline 판정: id=%s age=%ds", row["id"], int(age))
            except Exception:  # noqa: BLE001
                logger.exception("cameras is_online=false UPDATE 실패")
    return flipped


def scan_once() -> dict[str, int]:
    """1회 스캔. 통계 반환 (offline/recovered: devices, camera_offline: cameras)."""
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select("id, last_seen_at, is_online")
        .is_("unlinked_at", "null")            # 해제된 기기는 offline alert 대상 아님
        .execute()
    )
    rows = res.data or []

    now = _now()
    offline_count = 0
    recovered_count = 0

    for row in rows:
        device_uuid = row["id"]
        last_seen_raw = row.get("last_seen_at")
        was_online = bool(row.get("is_online"))

        if not last_seen_raw:
            # 페어링은 됐지만 한 번도 안 켜진 디바이스 — alert 안 띄움
            continue

        age = (now - _parse_iso(last_seen_raw)).total_seconds()

        if age > OFFLINE_THRESHOLD_SEC:
            # offline 판정
            if was_online:
                try:
                    sb.table("devices").update({"is_online": False}).eq(
                        "id", device_uuid
                    ).execute()
                except Exception:  # noqa: BLE001
                    logger.exception("is_online=false UPDATE 실패")
            _insert_offline_alert(sb, device_uuid, age)
            offline_count += 1
        else:
            # 정상 — 혹시 활성 offline alert 있으면 resolve
            _resolve_offline_alert(sb, device_uuid)
            if not was_online:
                recovered_count += 1

    camera_offline = _scan_cameras_offline(sb, now)
    return {
        "offline": offline_count,
        "recovered": recovered_count,
        "camera_offline": camera_offline,
    }


class OfflineMonitor:
    """별도 스레드에서 1분 주기 스캔. start()/stop() 으로 라이프사이클."""

    def __init__(self, interval_sec: float = DEFAULT_INTERVAL_SEC) -> None:
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="offline-monitor"
        )
        self._thread.start()
        logger.info("offline monitor 시작 (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("offline monitor 정지")

    def _run(self) -> None:
        last_supersede: datetime | None = None
        while not self._stop.is_set():
            try:
                stats = scan_once()
                if stats["offline"] or stats["recovered"] or stats.get("camera_offline"):
                    logger.info("offline scan: %s", stats)
            except Exception:  # noqa: BLE001
                logger.exception("offline scan 실패")
            now = _now()
            if last_supersede is None or (now - last_supersede).total_seconds() >= SUPERSEDE_INTERVAL_SEC:
                last_supersede = now
                try:
                    sstats = scan_supersede_once(now=now)
                    if any(sstats.values()):
                        logger.info("supersede scan: %s", sstats)
                except Exception:  # noqa: BLE001
                    # 컬럼 미적용(마이그레이션 전) 등 — offline 스캔은 영향 없이 계속
                    logger.exception("supersede scan 실패")
            self._stop.wait(self._interval)


__all__ = [
    "CAMERA_OFFLINE_THRESHOLD_SEC",
    "CRITICAL_THRESHOLD_SEC",
    "DEFAULT_INTERVAL_SEC",
    "OFFLINE_THRESHOLD_SEC",
    "SUPERSEDE_INTERVAL_SEC",
    "SUPERSEDE_OFFLINE_SEC",
    "SUPERSEDE_REVOKE_SEC",
    "OfflineMonitor",
    "scan_once",
    "scan_supersede_once",
]
