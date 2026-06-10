"""
MQTT 메시지 핸들러 — Stage A 비즈니스 로직.

bridge.py 의 paho 콜백이 호출. paho 의존 없는 순수 함수들이라
Supabase mock 만으로 단위 테스트 가능.

## 핸들러 책임

| 핸들러 | DB 작업 |
|--------|---------|
| handle_telemetry | telemetry INSERT + devices.last_seen_at/is_online UPDATE |
| handle_ack       | commands status='acked', result, acked_at UPDATE |
| handle_alert     | alerts INSERT |

## device_id 해상

`esp32/{device_id}/...` 의 device_id 는 TEXT — 두 종류 entity:
- 디바이스 (센서/제어): `terra-XXXXXXXX` → `devices` 테이블
- 카메라 워커: `p4cam-XXXXXXXX` / `picam-XXXXXXXX` → `cameras` 테이블

DB FK 는 각각 `devices.id` / `cameras.id` (UUID). 매 메시지마다 SELECT 하면 DB 왕복 비용 큼.
→ lru_cache 로 device_id_text → UUID 캐싱 (각 1000 entries).

`_resolve_entity()` 가 두 테이블을 순차 조회하고 (type, uuid) 튜플로 반환.

캐시 invalidation 은 token_rotate / 디바이스 삭제 시 별도 처리 필요 (Stage C/B).
지금은 캐시 만료 없음 — 운영 중 디바이스 식별자 변경 안 됨 (페어링 시점에만 결정).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from supabase import Client

from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


# ---------- device_id → UUID 캐시 ----------


@lru_cache(maxsize=1000)
def _cached_device_uuid(device_id_text: str) -> str | None:
    """devices.device_id (TEXT) → devices.id (UUID). 미존재면 None.

    sb 인자를 lru_cache key 에 안 넣기 위해 모듈 함수에서 get_supabase_client() 호출.
    """
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select("id")
        .eq("device_id", device_id_text)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return rows[0]["id"]


@lru_cache(maxsize=1000)
def _cached_device_text(device_uuid: str) -> str | None:
    """devices.id (UUID) → devices.device_id (TEXT). 미존재면 None.

    dispatcher 가 commands.device_id (UUID) → MQTT 토픽의 device_id (TEXT) 매핑할 때 사용.
    """
    sb = get_supabase_client()
    res = (
        sb.table("devices")
        .select("device_id")
        .eq("id", device_uuid)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return rows[0]["device_id"]


@lru_cache(maxsize=1000)
def _cached_camera_uuid(camera_id_text: str) -> str | None:
    """cameras.camera_id (TEXT, "p4cam-..." 등) → cameras.id (UUID). 미존재면 None."""
    sb = get_supabase_client()
    res = (
        sb.table("cameras")
        .select("id")
        .eq("camera_id", camera_id_text)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return rows[0]["id"]


def _resolve_entity(device_id_text: str) -> tuple[str | None, str | None]:
    """device_id (TEXT) → (entity_type, uuid).

    entity_type ∈ {"device", "camera", None}. 미페어링이면 (None, None).
    devices 를 먼저 조회 (대부분 디바이스 트래픽), 미스 시 cameras.
    """
    uid = _cached_device_uuid(device_id_text)
    if uid is not None:
        return ("device", uid)
    uid = _cached_camera_uuid(device_id_text)
    if uid is not None:
        return ("camera", uid)
    return (None, None)


def reset_device_cache() -> None:
    """테스트/디바이스 삭제 시 호출. devices/cameras 양쪽 캐시 모두 비움."""
    _cached_device_uuid.cache_clear()
    _cached_device_text.cache_clear()
    _cached_camera_uuid.cache_clear()


# ---------- ts 정규화 ----------

# epoch seconds 임계 (2017-07-14 이후) — 이보다 작으면 monotonic ms / 비정상 값으로 판단
_EPOCH_S_THRESHOLD = 1_500_000_000
# epoch ms 임계 (2017-07-14 이후)
_EPOCH_MS_THRESHOLD = 1_500_000_000_000


def _normalize_ts(raw: Any) -> str:
    """payload 의 ts → ISO8601 (UTC).

    값 형식 추론:
    - >1.5e12 → epoch ms (Unix timestamp ms)
    - >1.5e9  → epoch s
    - 그 외 (boot monotonic ms, None, 비정상) → 서버 NOW()

    SNTP 미동기화 디바이스도 그래도 동작하게 fallback.
    """
    if isinstance(raw, (int, float)) and raw > _EPOCH_MS_THRESHOLD:
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).isoformat()
    if isinstance(raw, (int, float)) and raw > _EPOCH_S_THRESHOLD:
        return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- 핸들러 ----------


def handle_telemetry(device_id_text: str, payload: dict[str, Any]) -> None:
    """
    스펙 ([docs/MQTT.md](../../docs/MQTT.md) §1):
        디바이스: { "ts": ..., "dht22_a": {...}, "dht22_b": {...},
                   "relay": "OFF", "fan": "ON", "heater": {"state":"OFF","locked":false} }
        카메라:   { "ts": ..., "uptime_sec": ..., "wifi_rssi": ..., "free_heap": ... }
                  (페이로드 자유 — 서버는 last_seen 만 갱신)

    동작:
    - device: telemetry INSERT + devices.last_seen_at/is_online UPDATE + 임계값 평가
    - camera: telemetry INSERT 건너뜀 (스키마 불일치). cameras.last_seen_at/is_online UPDATE 만.
    - 미페어링: 경고 후 무시.
    """
    entity_type, entity_uuid = _resolve_entity(device_id_text)
    if entity_uuid is None:
        logger.warning("telemetry: 미페어링 device_id=%s 무시", device_id_text)
        return

    sb = get_supabase_client()

    if entity_type == "camera":
        # 카메라는 heartbeat 만 — telemetry 행 INSERT X, last_seen 갱신 O.
        try:
            sb.table("cameras").update({
                "last_seen_at": _now_iso(),
                "is_online": True,
            }).eq("id", entity_uuid).execute()
        except Exception:  # noqa: BLE001
            logger.exception("cameras UPDATE 실패 (camera=%s)", device_id_text)
        return

    device_uuid = entity_uuid
    ts = _normalize_ts(payload.get("ts"))

    dht_a = payload.get("dht22_a") or {}
    dht_b = payload.get("dht22_b") or {}
    heater = payload.get("heater") or {}

    row = {
        "device_id": device_uuid,
        "ts": ts,
        "t_a": dht_a.get("t"),
        "h_a": dht_a.get("h"),
        "a_ok": bool(dht_a.get("ok", False)),
        "t_b": dht_b.get("t"),
        "h_b": dht_b.get("h"),
        "b_ok": bool(dht_b.get("ok", False)),
        "relay": payload.get("relay"),
        "fan": payload.get("fan"),
        "heater_state": heater.get("state"),
        "heater_locked": heater.get("locked"),
    }

    try:
        sb.table("telemetry").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — supabase-py 예외 타입 넓음
        # 동일 (device_id, ts) PK 충돌은 정상 (3초 주기 중복 전송). 그 외는 경고.
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            logger.debug("telemetry 중복 (device=%s ts=%s) — 무시", device_id_text, ts)
        else:
            logger.exception("telemetry INSERT 실패 (device=%s)", device_id_text)
        return

    # last_seen_at/is_online 갱신. 실패해도 telemetry 저장은 성공이라 별도 try.
    try:
        sb.table("devices").update({
            "last_seen_at": _now_iso(),
            "is_online": True,
        }).eq("id", device_uuid).execute()
    except Exception:  # noqa: BLE001
        logger.exception("devices UPDATE 실패 (device=%s)", device_id_text)

    # 임계값 평가 → alerts INSERT/RESOLVE (Stage D). 실패해도 telemetry 저장은 성공.
    # 임포트는 함수 안에서 — 순환참조 회피 (alerts.py 는 handlers 의존 안 함).
    try:
        from backend import alerts as alerts_mod
        alerts_mod.evaluate_telemetry(device_uuid, row)
    except Exception:  # noqa: BLE001
        logger.exception("alerts 평가 실패 (device=%s)", device_id_text)


def handle_ack(device_id_text: str, payload: dict[str, Any]) -> None:
    """
    스펙 ([docs/MQTT.md](../../docs/MQTT.md) §3):
        { "msg_id": "<uuid>", "result": "ok", "state": {...} }

    동작:
    - device ack: commands UPDATE status='acked', result, acked_at + devices.last_seen UPDATE
    - camera ack: cameras.last_seen UPDATE 만 (camera 대상 commands 테이블 없음 — webrtc 시그널링은
      별도의 short-lived MQTT 클라이언트가 직접 수신).
    """
    entity_type, entity_uuid = _resolve_entity(device_id_text)
    if entity_uuid is None:
        logger.warning("ack: 미페어링 device_id=%s 무시", device_id_text)
        return

    if entity_type == "camera":
        try:
            sb = get_supabase_client()
            sb.table("cameras").update({
                "last_seen_at": _now_iso(),
                "is_online": True,
            }).eq("id", entity_uuid).execute()
        except Exception:  # noqa: BLE001
            logger.exception("cameras UPDATE 실패 (ack camera=%s)", device_id_text)
        return

    device_uuid = entity_uuid
    msg_id = payload.get("msg_id")
    if not msg_id:
        logger.warning("ack: msg_id 없음 (device=%s, payload=%s)", device_id_text, payload)
        return

    result = payload.get("result", "ok")
    sb = get_supabase_client()

    try:
        res = (
            sb.table("commands")
            .update({
                "status": "acked",
                "result": result,
                "acked_at": _now_iso(),
            })
            .eq("id", msg_id)
            .eq("device_id", device_uuid)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("commands UPDATE 실패 (msg_id=%s)", msg_id)
        return

    if not res.data:
        logger.warning(
            "ack: 매칭되는 command 없음 (msg_id=%s, device=%s) — replay/foreign",
            msg_id, device_id_text,
        )

    # devices.last_seen_at 도 갱신 — ack 도 디바이스 살아있다는 신호
    try:
        sb.table("devices").update({
            "last_seen_at": _now_iso(),
            "is_online": True,
        }).eq("id", device_uuid).execute()
    except Exception:  # noqa: BLE001
        logger.exception("devices UPDATE 실패 (ack)")


def handle_alert(device_id_text: str, payload: dict[str, Any]) -> None:
    """
    스펙 ([docs/MQTT.md](../../docs/MQTT.md) §4):
        { "kind": "temp_high", "severity": "warning",
          "message": "...", "context": {...} }

    동작: alerts INSERT.
    """
    device_uuid = _cached_device_uuid(device_id_text)
    if device_uuid is None:
        logger.warning("alert: 미페어링 device_id=%s 무시", device_id_text)
        return

    kind = payload.get("kind")
    if not kind:
        logger.warning("alert: kind 없음 (device=%s, payload=%s)", device_id_text, payload)
        return

    sb = get_supabase_client()
    row = {
        "device_id": device_uuid,
        "kind": kind,
        "severity": payload.get("severity", "warning"),
        "message": payload.get("message"),
        "context": payload.get("context"),
    }

    try:
        sb.table("alerts").insert(row).execute()
    except Exception:  # noqa: BLE001
        logger.exception("alerts INSERT 실패 (device=%s)", device_id_text)


__all__ = [
    "_cached_camera_uuid",
    "_cached_device_text",
    "_resolve_entity",
    "handle_ack",
    "handle_alert",
    "handle_telemetry",
    "reset_device_cache",
]
