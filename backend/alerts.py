"""
알림(alerts) 평가 — Stage D.

handle_telemetry 가 INSERT 직후 호출. device_settings 임계값과 비교 →
이상 시 alerts INSERT, 정상화 시 자동 resolve.

## 평가 규칙

| kind | 트리거 | 정상화 |
|------|--------|--------|
| `temp_high`   | t_a > settings.alert_temp_high            | t_a <= threshold - 1°C (hysteresis) |
| `temp_low`    | t_a < settings.alert_temp_low             | t_a >= threshold + 1°C |
| `humid_low`   | h_a < settings.alert_humid_low            | h_a >= threshold + 5%RH |
| `sensor_fault`| dht22_a.ok == false (펌웨어 발 alert 와 별도, bridge 측 자체 평가) | a_ok = true |

> 펌웨어가 보낸 alert (heater_latched 등) 는 handle_alert 가 직접 INSERT.
> 본 모듈은 bridge 가 telemetry 보고 판단하는 derived alerts 만 처리.

## 중복 방지

같은 (device_id, kind) 의 활성 알림(resolved_at IS NULL) 있으면 INSERT 안 함.
정상화 시 해당 활성 알림에 resolved_at = NOW() UPDATE.

## hysteresis (히스테리시스)

임계값 ±α 로 ON/OFF 가르기 → 임계 근처 노이즈로 알림이 깜빡이는 것 방지.
- 온도: ±1°C
- 습도: ±5%RH
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

# 히스테리시스 폭
HYST_TEMP_C = 1.0
HYST_HUMID_PCT = 5.0


# ---------- device_settings 캐시 ----------

@lru_cache(maxsize=1000)
def _cached_settings(device_uuid: str) -> dict[str, Any] | None:
    """device_settings 1회 조회 + 캐싱. 변경 시 reset_settings_cache() 필요."""
    sb = get_supabase_client()
    res = (
        sb.table("device_settings")
        .select("alert_temp_high, alert_temp_low, alert_humid_low")
        .eq("device_id", device_uuid)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def reset_settings_cache() -> None:
    """테스트/설정 변경 시 호출."""
    _cached_settings.cache_clear()


# ---------- 활성 알림 조회/조작 ----------

def _active_alert(sb, device_uuid: str, kind: str) -> dict[str, Any] | None:
    res = (
        sb.table("alerts")
        .select("id")
        .eq("device_id", device_uuid)
        .eq("kind", kind)
        .is_("resolved_at", "null")
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _insert_alert(
    sb, device_uuid: str, kind: str, severity: str, message: str, context: dict[str, Any],
) -> None:
    if _active_alert(sb, device_uuid, kind):
        return  # dedup
    try:
        sb.table("alerts").insert({
            "device_id": device_uuid,
            "kind": kind,
            "severity": severity,
            "message": message,
            "context": context,
        }).execute()
        logger.info("alert INSERT: device=%s kind=%s", device_uuid, kind)
    except Exception:  # noqa: BLE001
        logger.exception("alert INSERT 실패: device=%s kind=%s", device_uuid, kind)


def _resolve_alert(sb, device_uuid: str, kind: str) -> None:
    active = _active_alert(sb, device_uuid, kind)
    if not active:
        return
    try:
        sb.table("alerts").update({
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", active["id"]).execute()
        logger.info("alert RESOLVED: device=%s kind=%s", device_uuid, kind)
    except Exception:  # noqa: BLE001
        logger.exception("alert resolve 실패: device=%s kind=%s", device_uuid, kind)


# ---------- 평가 진입점 ----------

def evaluate_telemetry(device_uuid: str, telemetry_row: dict[str, Any]) -> None:
    """handle_telemetry INSERT 직후 호출. 임계값과 비교 → 알림 발생/해제."""
    settings = _cached_settings(device_uuid)
    if not settings:
        return  # 설정 없으면 평가 안 함

    sb = get_supabase_client()
    t_a = telemetry_row.get("t_a")
    h_a = telemetry_row.get("h_a")
    a_ok = telemetry_row.get("a_ok", False)

    # 1) 센서 fault (DHT22-A)
    if not a_ok:
        _insert_alert(
            sb, device_uuid, "sensor_fault", "warning",
            "DHT22-A 통신 실패",
            {"sensor": "dht22_a"},
        )
        return  # 센서 fault 시 임계값 평가 의미 없음
    else:
        _resolve_alert(sb, device_uuid, "sensor_fault")

    # 2) 온도 high
    th_high = settings.get("alert_temp_high")
    if th_high is not None and t_a is not None:
        if t_a > th_high:
            _insert_alert(
                sb, device_uuid, "temp_high", "warning",
                f"온도 {t_a:.1f}°C — 임계 {th_high:.1f}°C 초과",
                {"t_a": t_a, "threshold": th_high},
            )
        elif t_a <= th_high - HYST_TEMP_C:
            _resolve_alert(sb, device_uuid, "temp_high")

    # 3) 온도 low
    th_low = settings.get("alert_temp_low")
    if th_low is not None and t_a is not None:
        if t_a < th_low:
            _insert_alert(
                sb, device_uuid, "temp_low", "warning",
                f"온도 {t_a:.1f}°C — 임계 {th_low:.1f}°C 미만",
                {"t_a": t_a, "threshold": th_low},
            )
        elif t_a >= th_low + HYST_TEMP_C:
            _resolve_alert(sb, device_uuid, "temp_low")

    # 4) 습도 low
    hu_low = settings.get("alert_humid_low")
    if hu_low is not None and h_a is not None:
        if h_a < hu_low:
            _insert_alert(
                sb, device_uuid, "humid_low", "warning",
                f"습도 {h_a:.1f}%RH — 임계 {hu_low:.1f}%RH 미만",
                {"h_a": h_a, "threshold": hu_low},
            )
        elif h_a >= hu_low + HYST_HUMID_PCT:
            _resolve_alert(sb, device_uuid, "humid_low")


__all__ = [
    "HYST_HUMID_PCT",
    "HYST_TEMP_C",
    "evaluate_telemetry",
    "reset_settings_cache",
]
