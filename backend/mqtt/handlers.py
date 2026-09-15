"""
MQTT 메시지 핸들러 — Stage A 비즈니스 로직.

bridge.py 의 paho 콜백이 호출. paho 의존 없는 순수 함수들이라
Supabase mock 만으로 단위 테스트 가능.

## 핸들러 책임

| 핸들러 | DB 작업 |
|--------|---------|
| handle_telemetry | telemetry INSERT + devices.last_seen_at/is_online UPDATE. 카메라는 last_seen + capabilities 저장 + rotate_180 동기화 |
| handle_ack       | commands status='acked', result, acked_at UPDATE |
| handle_alert     | alerts INSERT |

## device_id 해상

`esp32/{device_id}/...` 의 device_id 는 TEXT — 두 종류 entity:
- 디바이스 (센서/제어): `terra-XXXXXXXX` → `devices` 테이블
- 카메라 워커: `p4cam-XXXXXXXX` / `picam-XXXXXXXX` → `cameras` 테이블

DB FK 는 각각 `devices.id` / `cameras.id` (UUID). 매 메시지마다 SELECT 하면 DB 왕복 비용 큼.
→ TTL 캐시로 device_id_text ↔ UUID 캐싱.

`_resolve_entity()` 가 두 테이블을 순차 조회하고 (type, uuid) 튜플로 반환.

브리지는 API 서버와 별도 프로세스라 기기 삭제·이름 변경 신호를 받지 못한다. 그래서
만료 없는 lru_cache 대신 TTL 캐시를 쓴다 (성공 5분 / 미존재 15초). `reset_device_cache()`
는 같은 프로세스 안에서 즉시 비울 때만 쓴다(테스트).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from backend.mqtt.camera_commands import rotation_command
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


# ---------- 카메라 명령 발행기 (브리지가 등록) ----------
#
# handlers 는 paho 의존이 없어야 하므로 publish 함수를 주입받는다.
# MqttBridge.__init__ 이 set_command_publisher(self.publish_command) 로 등록.
# 미등록(테스트/단독 실행)이면 동기화 명령은 로그만 남기고 건너뛴다.

CommandPublisher = Callable[[str, dict[str, Any]], bool]
_command_publisher: CommandPublisher | None = None


def set_command_publisher(publisher: CommandPublisher | None) -> None:
    global _command_publisher
    _command_publisher = publisher


# ---------- device_id ↔ UUID 캐시 (TTL) ----------
#
# lru_cache 를 안 쓰는 이유: 브리지는 API 서버와 **별도 프로세스**라 기기 삭제·이름 변경
# 신호가 오지 않는다. 만료 없는 캐시면
#   - 삭제된 기기: 옛 UUID 를 계속 써서 telemetry INSERT 가 FK 위반으로 3초마다 실패
#   - 이름 변경: 푸시/로그에 옛 이름이 영원히 나감
# 이 되므로 시간 만료로만 최신값을 따라간다 (_camera_state_cache 와 같은 패턴).
#
# 실패(None)는 더 짧게 캐싱한다. 페어링 직후 첫 메시지가 캐시 미스로 None 을 박아두면
# 그 기기가 만료 전까지 통째로 무시되기 때문.

ENTITY_TTL_SEC = 300.0        # 5분 — 조회 성공
ENTITY_NEG_TTL_SEC = 15.0     # 15초 — 미존재(미페어링) 결과

_entity_lock = threading.Lock()
# key → (expires_monotonic, value)
_entity_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def _cache_get(kind: str, key: str) -> tuple[bool, Any]:
    """(hit, value). 만료됐거나 없으면 (False, None)."""
    now = time.monotonic()
    with _entity_lock:
        hit = _entity_cache.get((kind, key))
        if hit is not None and hit[0] > now:
            return (True, hit[1])
    return (False, None)


def _cache_put(kind: str, key: str, value: Any) -> None:
    ttl = ENTITY_TTL_SEC if value is not None else ENTITY_NEG_TTL_SEC
    with _entity_lock:
        _entity_cache[(kind, key)] = (time.monotonic() + ttl, value)


def _lookup_one(table: str, column: str, where_col: str, where_val: str) -> Any:
    """단일 컬럼 조회. 실패하면 None 이 아니라 예외를 올려 캐시 오염을 막는다."""
    sb = get_supabase_client()
    res = (
        sb.table(table)
        .select(column)
        .eq(where_col, where_val)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return rows[0][column]


def _cached_device_uuid(device_id_text: str) -> str | None:
    """devices.device_id (TEXT) → devices.id (UUID). 미존재면 None."""
    hit, val = _cache_get("device_uuid", device_id_text)
    if hit:
        return val
    try:
        val = _lookup_one("devices", "id", "device_id", device_id_text)
    except Exception:  # noqa: BLE001 — DB 장애를 "미페어링" 으로 굳히지 않는다
        logger.exception("devices 조회 실패 (device_id=%s)", device_id_text)
        return None
    _cache_put("device_uuid", device_id_text, val)
    return val


def _cached_device_text(device_uuid: str) -> str | None:
    """devices.id (UUID) → devices.device_id (TEXT). 미존재면 None.

    dispatcher 가 commands.device_id (UUID) → MQTT 토픽의 device_id (TEXT) 매핑할 때 사용.
    """
    hit, val = _cache_get("device_text", device_uuid)
    if hit:
        return val
    try:
        val = _lookup_one("devices", "device_id", "id", device_uuid)
    except Exception:  # noqa: BLE001
        logger.exception("devices 조회 실패 (uuid=%s)", device_uuid)
        return None
    _cache_put("device_text", device_uuid, val)
    return val


def _cached_camera_uuid(camera_id_text: str) -> str | None:
    """cameras.camera_id (TEXT, "p4cam-..." 등) → cameras.id (UUID). 미존재면 None."""
    hit, val = _cache_get("camera_uuid", camera_id_text)
    if hit:
        return val
    try:
        val = _lookup_one("cameras", "id", "camera_id", camera_id_text)
    except Exception:  # noqa: BLE001
        logger.exception("cameras 조회 실패 (camera_id=%s)", camera_id_text)
        return None
    _cache_put("camera_uuid", camera_id_text, val)
    return val


def device_meta(device_uuid: str) -> dict[str, Any] | None:
    """devices 행의 알림용 메타 (owner_id / name / enclosure_id). 미존재·실패면 None.

    푸시 이벤트가 device_name·enclosure_id 를 싣는데 commands 행에는 없어서 필요하다.
    TTL 캐시라 이름을 바꾸면 최대 ENTITY_TTL_SEC 만큼 옛 이름이 나갈 수 있다.
    """
    hit, val = _cache_get("device_meta", device_uuid)
    if hit:
        return val
    sb = get_supabase_client()
    try:
        res = (
            sb.table("devices")
            .select("owner_id, name, enclosure_id")
            .eq("id", device_uuid)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("devices 메타 조회 실패 (uuid=%s)", device_uuid)
        return None
    rows = res.data or []
    val = rows[0] if rows else None
    _cache_put("device_meta", device_uuid, val)
    return val


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
    with _entity_lock:
        _entity_cache.clear()
    with _camera_state_lock:
        _camera_state_cache.clear()
        _rotation_resend_at.clear()


# ---------- 카메라 설정 상태 캐시 (rotate_180 / capabilities) ----------
#
# 텔레메트리는 15초 주기라 매번 SELECT 하지 않도록 TTL 캐시. lru_cache 를 안 쓰는 이유:
# PATCH 는 API 프로세스, 텔레메트리는 브리지 프로세스라 무효화 신호가 안 오므로
# 시간 만료로만 최신값을 따라간다. 최악의 경우 PATCH 직후 명령이 유실됐을 때
# 수렴이 최대 TTL 만큼 늦어진다.

CAMERA_STATE_TTL_SEC = 30.0
# 카메라가 적용 실패로 계속 옛 값을 보고할 때 15초마다 재발행하지 않도록 최소 간격.
ROTATION_RESEND_MIN_SEC = 60.0

_camera_state_lock = threading.Lock()
_camera_state_cache: dict[str, tuple[float, dict[str, Any]]] = {}   # uuid → (expires, row)
_rotation_resend_at: dict[str, float] = {}                          # uuid → last publish monotonic


def _camera_state(sb: Client, camera_uuid: str) -> dict[str, Any] | None:
    """cameras.rotate_180 / capabilities 를 TTL 캐시 경유로 조회. 실패/미존재면 None."""
    now = time.monotonic()
    with _camera_state_lock:
        hit = _camera_state_cache.get(camera_uuid)
        if hit and hit[0] > now:
            return hit[1]
    try:
        res = (
            sb.table("cameras")
            .select("rotate_180, capabilities")
            .eq("id", camera_uuid)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("cameras 상태 조회 실패 (camera=%s)", camera_uuid)
        return None
    rows = res.data or []
    if not rows:
        return None
    row = {"rotate_180": rows[0].get("rotate_180"), "capabilities": rows[0].get("capabilities")}
    with _camera_state_lock:
        _camera_state_cache[camera_uuid] = (now + CAMERA_STATE_TTL_SEC, row)
    return row


def _camera_state_patch(camera_uuid: str, **fields: Any) -> None:
    """DB 에 UPDATE 한 값을 캐시에도 반영 (같은 값으로 반복 UPDATE 방지)."""
    with _camera_state_lock:
        hit = _camera_state_cache.get(camera_uuid)
        if hit:
            hit[1].update(fields)


def _sync_camera_rotation(
    sb: Client, camera_id_text: str, camera_uuid: str, reported: bool
) -> None:
    """카메라가 보고한 rotate_180 이 DB(진실)와 다르면 set_rotation 재발행.

    PATCH 직후 명령이 유실됐거나(오프라인·QoS1 비보존), 재부팅 후 NVS 값이 다르거나,
    펌웨어를 교체한 경우 모두 여기서 DB 값으로 수렴한다. 구 펌웨어는 rotate_180 을
    보고하지 않으므로 호출되지 않는다.
    """
    state = _camera_state(sb, camera_uuid)
    if state is None or not isinstance(state.get("rotate_180"), bool):
        return
    desired: bool = state["rotate_180"]
    if desired == reported:
        return

    now = time.monotonic()
    with _camera_state_lock:
        last = _rotation_resend_at.get(camera_uuid, 0.0)
        if now - last < ROTATION_RESEND_MIN_SEC:
            return
        _rotation_resend_at[camera_uuid] = now

    if _command_publisher is None:
        logger.warning(
            "rotate_180 불일치 camera=%s (보고=%s, DB=%s) — 발행기 미등록, 건너뜀",
            camera_id_text, reported, desired,
        )
        return
    ok = _command_publisher(camera_id_text, rotation_command(desired))
    logger.warning(
        "rotate_180 불일치 camera=%s (보고=%s, DB=%s) → set_rotation 재발행 %s",
        camera_id_text, reported, desired, "ok" if ok else "실패",
    )


# ---------- 센서 값 정규화 ----------


def _sensor_values(dht: dict[str, Any]) -> tuple[float | None, float | None, bool]:
    """DHT22 한 채널 → (온도, 습도, ok).

    `ok: false` 는 센서 fault 이고 이때 t/h 는 무의미하다(docs/MQTT.md §1).
    예전에는 그 값을 그대로 INSERT 해서 telemetry_30m 평균·최소·최대가 오염됐다
    (2026-09-15 앱 회신 §5.3). 이제 fault 면 NULL 로 저장한다 — a_ok/b_ok 자체는
    그대로 남으니 "고장이었다"는 정보는 잃지 않는다.

    롤업 SQL 쪽에도 FILTER (WHERE a_ok) 를 넣어 이중으로 막는다
    (migrations/2026-09-15_telemetry_30m_valid_counts.sql). 이미 쌓인 오염 행이
    원본 보존기간(7일) 동안 남아 있기 때문.
    """
    ok = bool(dht.get("ok", False))
    if not ok:
        return (None, None, False)
    return (dht.get("t"), dht.get("h"), True)


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
                   "relay": "OFF", "fan": "ON", "fan2": "OFF",
                   "heater": {"state":"OFF","locked":false},
                   "led": "ON", "led_brightness": 75 }  # led_brightness 는 MOSFET 보드만
        카메라:   { "ts": ..., "uptime_sec": ..., "free_heap": ...,
                   "rotate_180": false,                      # 현재 NVS 값 (2026-09-08+)
                   "capabilities": {"rotate_180": true} }     # 연결 직후 1회일 수 있음 (2026-09-08+)

    동작:
    - device: telemetry INSERT + devices.last_seen_at/is_online UPDATE + 임계값 평가
    - camera: telemetry INSERT 건너뜀 (스키마 불일치). cameras.last_seen_at/is_online UPDATE.
              capabilities 가 있고 DB 와 다르면 함께 저장. rotate_180 보고값이 DB 와 다르면
              set_rotation 재발행 (_sync_camera_rotation).
    - 미페어링: 경고 후 무시.
    """
    entity_type, entity_uuid = _resolve_entity(device_id_text)
    if entity_uuid is None:
        logger.warning("telemetry: 미페어링 device_id=%s 무시", device_id_text)
        return

    sb = get_supabase_client()

    if entity_type == "camera":
        # 카메라는 heartbeat 만 — telemetry 행 INSERT X, last_seen 갱신 O.
        update: dict[str, Any] = {
            "last_seen_at": _now_iso(),
            "is_online": True,
        }

        # capabilities 보고 → 저장. 같은 값이면 UPDATE 에서 빼서 Realtime UPDATE 잡음을
        # 줄인다 (앱이 cameras 테이블 Realtime 을 구독 중).
        caps = payload.get("capabilities")
        if isinstance(caps, dict):
            state = _camera_state(sb, entity_uuid)
            if state is None or state.get("capabilities") != caps:
                update["capabilities"] = caps

        try:
            sb.table("cameras").update(update).eq("id", entity_uuid).execute()
        except Exception:  # noqa: BLE001
            logger.exception("cameras UPDATE 실패 (camera=%s)", device_id_text)
        else:
            if "capabilities" in update:
                _camera_state_patch(entity_uuid, capabilities=update["capabilities"])

        reported = payload.get("rotate_180")
        if isinstance(reported, bool):
            try:
                _sync_camera_rotation(sb, device_id_text, entity_uuid, reported)
            except Exception:  # noqa: BLE001
                logger.exception("rotate_180 동기화 실패 (camera=%s)", device_id_text)
        return

    device_uuid = entity_uuid
    ts = _normalize_ts(payload.get("ts"))

    dht_a = payload.get("dht22_a") or {}
    dht_b = payload.get("dht22_b") or {}
    heater = payload.get("heater") or {}

    t_a, h_a, a_ok = _sensor_values(dht_a)
    t_b, h_b, b_ok = _sensor_values(dht_b)

    row = {
        "device_id": device_uuid,
        "ts": ts,
        "t_a": t_a,
        "h_a": h_a,
        "a_ok": a_ok,
        "t_b": t_b,
        "h_b": h_b,
        "b_ok": b_ok,
        "relay": payload.get("relay"),
        "fan": payload.get("fan"),
        "fan2": payload.get("fan2"),                     # 냉각팬 — 구 펌웨어는 None
        "heater_state": heater.get("state"),
        "heater_locked": heater.get("locked"),
        "led": payload.get("led"),                       # 'ON' | 'OFF' | None (§4)
        "led_brightness": payload.get("led_brightness"),  # 0~100 (MOSFET), 릴레이는 None
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
