"""
오프라인 감시 — Stage D.

매 1분 주기로 last_seen_at 검사 → 3분 이상 무응답이면:
- devices: is_online = false + alerts INSERT (kind='offline')
- cameras: is_online = false 만 (카메라용 alert 파이프라인 없음)
재연결 시 handle_telemetry/handle_ack 가 is_online=true 로 자동 복원하고,
본 모니터가 다음 주기에 offline alert 를 resolve.

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
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60.0
OFFLINE_THRESHOLD_SEC = 180  # 3분 (디바이스: telemetry 3초 주기 기준)
CRITICAL_THRESHOLD_SEC = 600  # 10분
# 카메라는 telemetry 가 15초 주기(MQTT_TELEMETRY_PERIOD_MS)이고 QoS0(유실 가능)라 더 여유를 둔다.
# 실측상 정상 카메라도 일시적으로 ~400초 telemetry 공백이 났다(라이브 직후 복귀). 너무 빡빡하면
# 정상 카메라가 깜빡 offline 으로 튀므로 10분으로 잡는다. 진짜 죽은 카메라(수 시간~일)는 충분히 걸린다.
CAMERA_OFFLINE_THRESHOLD_SEC = 600  # 10분


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


def _scan_cameras_offline(sb, now: datetime) -> int:
    """카메라 is_online 갱신 (offline 판정만).

    카메라는 bridge 가 telemetry/ack 로 is_online=True 만 세팅하고, 어디서도 False 로
    되돌리지 않아 한 번 online 되면 영원히 online 으로 박힌다(웹 표시가 거짓말). devices 와 달리
    카메라용 alert 파이프라인은 없으므로 여기선 **is_online=False 플립만** 한다.
    재연결 시 handle_telemetry/handle_ack 가 다시 True 로 복원한다.
    """
    res = sb.table("cameras").select("id, last_seen_at, is_online").execute()
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
        while not self._stop.is_set():
            try:
                stats = scan_once()
                if stats["offline"] or stats["recovered"] or stats.get("camera_offline"):
                    logger.info("offline scan: %s", stats)
            except Exception:  # noqa: BLE001
                logger.exception("offline scan 실패")
            self._stop.wait(self._interval)


__all__ = [
    "CAMERA_OFFLINE_THRESHOLD_SEC",
    "CRITICAL_THRESHOLD_SEC",
    "DEFAULT_INTERVAL_SEC",
    "OFFLINE_THRESHOLD_SEC",
    "OfflineMonitor",
    "scan_once",
]
