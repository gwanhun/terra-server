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

`esp32/{device_id}/...` 의 device_id 는 TEXT (e.g. "terra-a1b2c3d4").
DB FK 는 `devices.id` (UUID). 매 메시지마다 SELECT 하면 DB 왕복 비용 큼.
→ lru_cache 로 device_id_text → UUID 캐싱 (1000 entries).

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


def reset_device_cache() -> None:
    """테스트/디바이스 삭제 시 호출."""
    _cached_device_uuid.cache_clear()


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
        { "ts": ..., "dht22_a": {...}, "dht22_b": {...},
          "relay": "OFF", "fan": "ON", "heater": {"state":"OFF","locked":false} }

    동작: telemetry INSERT + devices.last_seen_at/is_online UPDATE.
    device_id 미존재 시 경고 후 무시 (페어링 안 된 디바이스).
    """
    device_uuid = _cached_device_uuid(device_id_text)
    if device_uuid is None:
        logger.warning("telemetry: 미페어링 device_id=%s 무시", device_id_text)
        return

    sb = get_supabase_client()
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


def handle_ack(device_id_text: str, payload: dict[str, Any]) -> None:
    """
    스펙 ([docs/MQTT.md](../../docs/MQTT.md) §3):
        { "msg_id": "<uuid>", "result": "ok", "state": {...} }

    동작: commands UPDATE status='acked', result, acked_at.
    msg_id 가 본인 디바이스의 commands 가 아니면 매칭 0건이라 자연스럽게 무시.
    """
    device_uuid = _cached_device_uuid(device_id_text)
    if device_uuid is None:
        logger.warning("ack: 미페어링 device_id=%s 무시", device_id_text)
        return

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
    "handle_ack",
    "handle_alert",
    "handle_telemetry",
    "reset_device_cache",
]
