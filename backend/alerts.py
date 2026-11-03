"""
알림(alerts) 평가 — Stage D(legacy) + Stage N(v4 온습도, `ALERTS_V4_ENABLED`).

handle_telemetry 가 INSERT 직후 호출(paho 콜백 스레드, 동기). 경로는 플래그로 하나만 돈다.

- `ALERTS_V4_ENABLED` 가 true/1/yes/on → **v4** (기획 v4 1-1~1-6, specs/stage-n-env-alerts-v4.md)
- 그 외(기본) → **legacy** (`alert_*` 컬럼 기반, Stage D 그대로)

## 평가 규칙

| 항목 | v4 (플래그 켬) | legacy (기본) |
|------|----------------|---------------|
| kind | `env_temp_high` `env_temp_low` `env_humid_low` `env_humid_high` (`context.rule="v4"`) | `temp_high` `temp_low` `humid_low` |
| 기준 | `device_settings.target_*` MIN/MAX(없으면 18~26℃ / 40~80%), 경고 15/29℃ 고정 | `device_settings.alert_*` (행 없으면 평가 안 함) |
| 진입 | 조건 2분 연속(과습 60분). 주의 `warning` / 경고 `critical` | 임계 넘는 즉시, 항상 `warning` |
| 회복 | 적정 5분 연속(값 여유 없음) | 히스테리시스 ±1℃ / +5%RH 즉시 |
| 기록 시각 | `triggered_at`/`resolved_at` = 조건 연속 성립 **시작** 시각 | DB 기본값(now) |
| 푸시 | `env.alert.triggered` started + 1시간 뒤 reminder 1회 (`PUSH_ENV_ALERTS_ENABLED`) | 없음 |
| 재시작 | 활성 v4 행을 복원(푸시·추가 푸시 중복 없음) | 활성 행 dedup 조회 |
| `sensor_fault` | dht22_a.ok == false → INSERT, 복구 시 resolve — **두 경로 공통** | (같음) |

판정 상태 기계는 `backend/env_rules.py`(순수), 이 모듈은 설정 로드·복원·DB 기록·푸시 적재만 한다.

> 펌웨어가 보낸 alert (heater_latched, 펌웨어 temp_high 등) 는 handle_alert 가 직접 INSERT.
> 본 모듈은 bridge 가 telemetry 보고 판단하는 derived alerts 만 처리. v4 는 kind 를 따로 써서
> 펌웨어 행과 복원·종료가 섞이지 않는다(D6).

## v4 기록 규칙 (Task 2)

- 설정: 기기별 60초 TTL 캐시. 조회 실패 → 직전 값(없으면 그 샘플 보류). 바뀐 요소는 타이머 초기화(D8).
- 복원: 기기 첫 평가 때 활성 v4 행을 요소별 최신 1행만 이어받고 나머지는 종료. 구 브리지 판정 행
  (`temp_high` 등 + `context.threshold`, `rule` 없음)도 종료. 조회 실패면 그 샘플 보류.
- open INSERT 실패 → 트래커를 그 step 이전 활성으로 되돌린다. 같은 step 의 close(전환·완화)도
  커밋하지 않으므로 다음 샘플이 같은 전이를 다시 낸다(무음 완화가 푸시 주의로 바뀌는 일 없음).
- 행 UPDATE(종료·pushed_at·peak 등) 실패 → 기기별 재시도 목록, 다음 샘플에서 다시 쓴다.
- 푸시 적재 실패(`failed`) → 10분 동안 다음 샘플마다 재시도(event_id 멱등, D16). 스위치 꺼짐·
  소유자 없음은 기록만. `pushed_at`/`reminded_at` 은 적재 성공(queued/duplicate) 때만.
- 기기 해제(`devices.unlinked_at`) 확인 → 활성 v4 행 종료 + 평가 중단(D12).

## legacy 중복 방지 / 히스테리시스

같은 (device_id, kind) 의 활성 알림(resolved_at IS NULL) 있으면 INSERT 안 함.
정상화 시 해당 활성 알림에 resolved_at = NOW() UPDATE.
임계값 ±α 로 ON/OFF 가르기 → 임계 근처 노이즈로 알림이 깜빡이는 것 방지 (온도 ±1°C, 습도 ±5%RH).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from backend import push_events
from backend.env_rules import (
    Action,
    ActiveEvent,
    ElementTracker,
    Thresholds,
    display_diff,
    title,
)
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

# 히스테리시스 폭 (legacy)
HYST_TEMP_C = 1.0
HYST_HUMID_PCT = 5.0

# v4
V4_KINDS = ("env_temp_high", "env_temp_low", "env_humid_low", "env_humid_high")
LEGACY_BRIDGE_KINDS = ("temp_high", "temp_low", "humid_low")
SETTINGS_TTL_SEC = 60.0      # D8
SETTINGS_RETRY_SEC = 30.0    # 설정 조회 실패 시 다음 재조회까지(장애 중 샘플마다 묻지 않게)
PUSH_RETRY_SEC = 600.0       # D16 적재 실패 재시도 창
_SEVERITY = {"caution": "warning", "danger": "critical"}
_ELEMENTS = ("temp", "humid")
# 구 브리지 판정(_evaluate_legacy)이 쓰는 context 키 집합 — 펌웨어 행과 구분
_LEGACY_CTX_KEYS = ({"t_a", "threshold"}, {"h_a", "threshold"})


def v4_enabled() -> bool:
    """`ALERTS_V4_ENABLED` 가 true/1/yes/on 일 때만 v4. 기본 꺼짐(앱 확인 전 운영 동작 불변)."""
    raw = (os.getenv("ALERTS_V4_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# 테스트가 바꿔 끼우는 시계 (D7: 경과는 monotonic, 기록은 wall)
def _now_mono() -> float:
    return time.monotonic()


def _now_wall() -> datetime:
    return datetime.now(timezone.utc)


# ---------- device_settings 캐시 (legacy) ----------

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
    """테스트/설정 변경 시 호출. legacy 캐시 + v4 TTL 캐시(다음 샘플에서 재조회)."""
    _cached_settings.cache_clear()
    with _v4_lock:
        for st in _states.values():
            st.th_expires = 0.0


# ---------- 활성 알림 조회/조작 (legacy + sensor_fault) ----------

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
) -> bool:
    """활성 행이 있거나 INSERT 성공이면 True. legacy 는 반환값을 안 쓴다."""
    if _active_alert(sb, device_uuid, kind):
        return True  # dedup
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
        return False
    return True


def _resolve_alert(sb, device_uuid: str, kind: str) -> bool:
    """활성 행이 없거나 UPDATE 성공이면 True."""
    active = _active_alert(sb, device_uuid, kind)
    if not active:
        return True
    try:
        sb.table("alerts").update({
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", active["id"]).execute()
        logger.info("alert RESOLVED: device=%s kind=%s", device_uuid, kind)
    except Exception:  # noqa: BLE001
        logger.exception("alert resolve 실패: device=%s kind=%s", device_uuid, kind)
        return False
    return True


def _evaluate_sensor_fault(sb, device_uuid: str, a_ok: Any) -> bool:
    """legacy: DHT22-A fault → sensor_fault INSERT / 복구 시 resolve (샘플마다). a_ok 를 돌려준다.

    v4 는 같은 규칙을 상태가 바뀔 때만 확인한다(`_sensor_fault_v4`).
    """
    if not a_ok:
        _insert_alert(
            sb, device_uuid, "sensor_fault", "warning",
            "DHT22-A 통신 실패",
            {"sensor": "dht22_a"},
        )
        return False
    _resolve_alert(sb, device_uuid, "sensor_fault")
    return True


# ---------- 평가 진입점 ----------

def evaluate_telemetry(
    device_uuid: str, telemetry_row: dict[str, Any], device_key: str | None = None,
) -> None:
    """handle_telemetry INSERT 직후 호출. 플래그에 따라 v4 또는 legacy 하나만 돈다.

    device_key: MQTT client_id(terra-xxxxxxxx) — v4 푸시 payload 용. legacy 는 안 쓴다.
    """
    if v4_enabled():
        _evaluate_v4(device_uuid, telemetry_row, device_key)
    else:
        _evaluate_legacy(device_uuid, telemetry_row)


def _evaluate_legacy(device_uuid: str, telemetry_row: dict[str, Any]) -> None:
    """Stage D 판정(현행 그대로). 임계값과 비교 → 알림 발생/해제."""
    settings = _cached_settings(device_uuid)
    if not settings:
        return  # 설정 없으면 평가 안 함

    sb = get_supabase_client()
    t_a = telemetry_row.get("t_a")
    h_a = telemetry_row.get("h_a")

    # 1) 센서 fault (DHT22-A) — 센서 fault 시 임계값 평가 의미 없음
    if not _evaluate_sensor_fault(sb, device_uuid, telemetry_row.get("a_ok", False)):
        return

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


# =====================================================================
# v4 경로
# =====================================================================

@dataclass
class _PendingPush:
    """적재 못 한 started 푸시(D16). first_mono 부터 PUSH_RETRY_SEC 동안 재시도."""

    alert_id: str
    kind: str
    level: str
    title: str
    value: float
    threshold: float | None
    first_mono: float


@dataclass
class _DeviceState:
    trackers: dict[str, ElementTracker] = field(
        default_factory=lambda: {e: ElementTracker(e) for e in _ELEMENTS})  # type: ignore[arg-type]
    restored: bool = False
    th: Thresholds | None = None
    th_expires: float = 0.0
    # alert_id → 그 행의 context (DB 와 같게 유지). jsonb 는 통째로 덮어쓰므로 병합은 여기서.
    ctx: dict[str, dict[str, Any]] = field(default_factory=dict)
    # alert_id → 써야 하는데 실패한 행 패치(context 제외 — context 는 ctx 에서). 다음 샘플 재시도.
    dirty: dict[str, dict[str, Any]] = field(default_factory=dict)
    push_retry: dict[str, _PendingPush] = field(default_factory=dict)   # element → started
    remind_fail_since: dict[str, float] = field(default_factory=dict)   # alert_id → 첫 실패 mono
    unlinked: bool = False
    # sensor_fault 활성 여부(None = 모름). a_ok 가 바뀔 때만 DB 를 본다 — 정상 샘플마다 SELECT 안 함.
    fault_active: bool | None = None


_v4_lock = threading.Lock()
_states: dict[str, _DeviceState] = {}


def reset_state() -> None:
    """테스트용 — 기기별 v4 판정 상태 전부 비움."""
    with _v4_lock:
        _states.clear()


@dataclass
class _Step:
    """샘플 1건 처리에 공통으로 쓰는 것들."""

    sb: Any
    st: _DeviceState
    device_uuid: str
    device_key: str | None
    meta: dict[str, Any] | None
    now_mono: float
    now_wall: datetime

    def wall(self, at_mono: float) -> datetime:
        """monotonic 시각 → 기록용 wall clock (D7, D11)."""
        return self.now_wall - timedelta(seconds=self.now_mono - at_mono)


# ---------- 작은 도우미 ----------

def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _element_of(kind: str) -> str:
    return "temp" if kind.startswith("env_temp") else "humid"


def _is_high(kind: str) -> bool:
    return kind.endswith("_high")


def _device_meta(device_uuid: str) -> dict[str, Any] | None:
    # 지연 임포트 — handlers 가 이 모듈을 함수 안에서 임포트한다(순환 회피).
    from backend.mqtt import handlers
    return handlers.device_meta(device_uuid)


def _iso_mono(c: _Step, mono: float | None) -> str | None:
    return c.wall(mono).isoformat() if mono is not None else None


def forced_close_context(row: dict[str, Any]) -> dict[str, Any]:
    """강제 종료(중복 정리·기기 해제·close_v4_alerts.py)용 context.

    `context.peak_value` 가 있으면 `peak_diff` 를 같이 남긴다 — 분석이 "+None" 을 만들지 않게.
    """
    ctx = dict(row.get("context") or {})
    peak = _num(ctx.get("peak_value"))
    thr = _num(ctx.get("threshold"))
    if row.get("kind") in V4_KINDS and peak is not None and thr is not None:
        ctx["peak_diff"] = display_diff(row["kind"], peak, thr)
    return ctx


def select_active_v4_rows(sb: Any, device_uuid: str | None = None) -> list[dict[str, Any]]:
    """활성 v4 행(kind 4종 정확히 + context.rule == "v4"). 예외는 호출측이 처리.

    `context->>rule` 필터는 파이썬에서 건다 — 활성 행은 기기당 몇 개라 비용이 없고,
    테스트 가짜(FakeSB)와 같은 쿼리를 쓸 수 있다. 접두어 매칭은 하지 않는다(Stage O 의 env_vent_* 제외).
    """
    q = (
        sb.table("alerts")
        .select("id, device_id, kind, severity, triggered_at, context")
        .is_("resolved_at", "null")
        .in_("kind", list(V4_KINDS))
    )
    if device_uuid is not None:
        q = q.eq("device_id", device_uuid)
    rows = q.execute().data or []
    return [r for r in rows if isinstance(r.get("context"), dict) and r["context"].get("rule") == "v4"]


def close_v4_row(sb: Any, row: dict[str, Any], resolved_at: str) -> None:
    """활성 v4 행 1개 강제 종료(peak_diff 포함). 예외는 호출측(해제 API·롤백 스크립트)이 처리."""
    sb.table("alerts").update({"resolved_at": resolved_at, "context": forced_close_context(row)}) \
        .eq("id", row["id"]).execute()


# ---------- 행 쓰기 (실패 시 재시도 목록) ----------

def _write(sb: Any, st: _DeviceState, alert_id: str, *,
           row: dict[str, Any] | None = None, ctx: dict[str, Any] | None = None) -> bool:
    """alerts 행 UPDATE. context 는 메모리 사본에 병합해 통째로 쓴다. 실패 → st.dirty."""
    merged = dict(st.ctx.get(alert_id) or {})
    if ctx:
        merged.update(ctx)
    st.ctx[alert_id] = merged
    patch = dict(st.dirty.pop(alert_id, {}))
    if row:
        patch.update(row)
    try:
        sb.table("alerts").update({**patch, "context": merged}).eq("id", alert_id).execute()
    except Exception:  # noqa: BLE001 — supabase-py 예외 타입 넓음
        logger.exception("v4 alert UPDATE 실패 — 다음 샘플 재시도 (alert=%s)", alert_id)
        st.dirty[alert_id] = patch
        return False
    if "resolved_at" in patch:
        st.ctx.pop(alert_id, None)   # 닫힌 행은 더 쓰지 않는다
    return True


def _flush_dirty(sb: Any, st: _DeviceState) -> None:
    for alert_id in list(st.dirty):
        _write(sb, st, alert_id)


def _force_close(sb: Any, st: _DeviceState, row: dict[str, Any], now_wall: datetime) -> None:
    st.ctx[row["id"]] = forced_close_context(row)
    _write(sb, st, row["id"], row={"resolved_at": now_wall.isoformat()})
    logger.info("v4 alert 강제 종료: device=%s kind=%s id=%s", row.get("device_id"), row.get("kind"), row["id"])


# ---------- 복원 / 설정 ----------

def _to_active(row: dict[str, Any], now_mono: float, now_wall: datetime) -> ActiveEvent:
    ctx = row.get("context") or {}
    kind = row["kind"]
    level = ctx.get("level")
    if level not in ("caution", "danger"):
        level = "danger" if row.get("severity") == "critical" else "caution"
    started = _parse_ts(row.get("triggered_at"))
    def mono(ts: datetime | None) -> float | None:
        return now_mono - (now_wall - ts).total_seconds() if ts else None

    chain_pushed = mono(_parse_ts(ctx.get("chain_pushed_at")))
    chain_reminded = bool(ctx.get("chain_reminded"))
    pushed_mono = mono(_parse_ts(ctx.get("pushed_at")))
    reminded = ctx.get("reminded_at") is not None
    silent = ctx.get("deescalated_from") is not None
    silent_since = mono(started) if silent else None
    if ctx.get("push_suppressed") and pushed_mono is None:   # D14 재경고: 체인 기준 추가 푸시
        pushed_mono = chain_pushed
        reminded = reminded or chain_reminded
    peak = _num(ctx.get("peak_value"))
    if peak is None:
        peak = _num(ctx.get("value"))
    if peak is None:   # 기록이 없으면 다음 샘플이 곧 채우도록 반대 극값
        peak = -math.inf if _is_high(kind) else math.inf
    return ActiveEvent(
        kind=kind, level=level, started_wall=started, pushed_mono=pushed_mono,
        reminded=reminded, silent=silent, peak=peak,
        alert_id=row["id"], silent_since_mono=silent_since,
        chain_pushed_mono=chain_pushed if silent else None,
        chain_reminded=chain_reminded if silent else False,
    )


def _restore(c: _Step) -> bool:
    """기기 첫 v4 평가. 실패면 False(복원 완료 표시 안 함 → 다음 샘플 재시도)."""
    try:
        res = (
            c.sb.table("alerts")
            .select("id, device_id, kind, severity, triggered_at, context")
            .eq("device_id", c.device_uuid)
            .is_("resolved_at", "null")
            .in_("kind", [*V4_KINDS, *LEGACY_BRIDGE_KINDS])
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("v4 복원 조회 실패 — 이 샘플 평가 보류 (device=%s)", c.device_uuid)
        return False
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    latest: dict[str, dict[str, Any]] = {}
    to_close: list[dict[str, Any]] = []
    for r in res.data or []:
        ctx = r.get("context") if isinstance(r.get("context"), dict) else {}
        if r["kind"] in V4_KINDS and ctx.get("rule") == "v4":
            el = _element_of(r["kind"])
            cur = latest.get(el)
            if cur is None:
                latest[el] = r
            elif (_parse_ts(r.get("triggered_at")) or oldest) > (_parse_ts(cur.get("triggered_at")) or oldest):
                to_close.append(cur)
                latest[el] = r
            else:
                to_close.append(r)
        elif r["kind"] in LEGACY_BRIDGE_KINDS and set(ctx) in _LEGACY_CTX_KEYS:
            to_close.append(r)    # 구 브리지 판정 행. 펌웨어 행(context 가 다름)은 건드리지 않음
    for r in to_close:
        _force_close(c.sb, c.st, r, c.now_wall)
    for el, r in latest.items():
        c.st.ctx[r["id"]] = dict(r.get("context") or {})
        c.st.trackers[el].restore(_to_active(r, c.now_mono, c.now_wall), c.now_mono)
    c.st.restored = True
    return True


def _thresholds(c: _Step) -> Thresholds | None:
    st = c.st
    if c.now_mono < st.th_expires:
        return st.th   # 캐시 유효 또는 실패 backoff 중(그동안 th 가 None 이면 보류)
    try:
        res = (
            c.sb.table("device_settings")
            .select("target_temp_min, target_temp_max, target_humid_min, target_humid_max")
            .eq("device_id", c.device_uuid)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("device_settings 조회 실패 — 직전 기준 유지 (device=%s): %s", c.device_uuid, exc)
        st.th_expires = c.now_mono + SETTINGS_RETRY_SEC
        return st.th   # 없으면 None → 보류. 30초 뒤 재조회
    rows = res.data or []
    new = Thresholds.sanitize(rows[0] if rows else None)
    old = st.th
    if old is not None:
        if (old.temp_min, old.temp_max) != (new.temp_min, new.temp_max):
            st.trackers["temp"].reset_timers()
        if (old.humid_min, old.humid_max) != (new.humid_min, new.humid_max):
            st.trackers["humid"].reset_timers()
    st.th = new
    st.th_expires = c.now_mono + SETTINGS_TTL_SEC
    return new


def _sample_values(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """a_ok=false → 둘 다 None. 아니면 숫자가 아니거나 NaN 인 요소만 None (D2)."""
    if not row.get("a_ok"):
        return None, None
    return _num(row.get("t_a")), _num(row.get("h_a"))


# ---------- 푸시 ----------

def _enqueue(c: _Step, alert_id: str, kind: str, level: str, phase: str,
             title_: str, value: float, threshold: float | None) -> str:
    """queued / duplicate / disabled(스위치 꺼짐·소유자 없음 — 기록만) / failed(재시도)."""
    if not push_events.env_alerts_push_enabled():
        return "disabled"
    if c.meta is None:
        return "failed"    # devices 메타 조회 실패 — 소유자를 모르는 것이지 없는 게 아니다
    ev = push_events.build_env_alert_event(
        alert_id=alert_id, kind=kind, level=level, phase=phase,  # type: ignore[arg-type]
        title=title_, value=value, threshold=threshold,
        device_id=c.device_uuid, device_key=c.device_key, device_meta=c.meta,
    )
    if ev is None:
        return "disabled"
    return push_events.enqueue_env_alert(ev)


def _push_started(c: _Step, element: str, p: _PendingPush) -> None:
    res = _enqueue(c, p.alert_id, p.kind, p.level, "started", p.title, p.value, p.threshold)
    if res in ("queued", "duplicate"):
        c.st.push_retry.pop(element, None)
        c.st.trackers[element].mark_pushed(c.now_mono)
        _write(c.sb, c.st, p.alert_id, ctx={"pushed_at": c.now_wall.isoformat()})
    elif res == "failed" and c.now_mono - p.first_mono < PUSH_RETRY_SEC:
        c.st.push_retry[element] = p
    else:
        if res == "failed":
            logger.warning("v4 started 푸시 재시도 포기(10분) alert=%s", p.alert_id)
        c.st.push_retry.pop(element, None)


def _retry_pushes(c: _Step) -> None:
    for element, p in list(c.st.push_retry.items()):
        active = c.st.trackers[element].active
        if active is None or active.alert_id != p.alert_id or c.now_mono - p.first_mono >= PUSH_RETRY_SEC:
            c.st.push_retry.pop(element, None)   # 이벤트가 끝났거나 창이 지남
            continue
        _push_started(c, element, p)


def _remind(c: _Step, tracker: ElementTracker, act: Action) -> None:
    a = tracker.active
    if a is None or a.alert_id is None or act.value is None:
        return
    res = _enqueue(c, a.alert_id, a.kind, a.level, "reminder",
                   title(a.kind, a.level, act.value, act.threshold), act.value, act.threshold)
    if res in ("queued", "duplicate"):
        c.st.remind_fail_since.pop(a.alert_id, None)
        _write(c.sb, c.st, a.alert_id, ctx={"reminded_at": c.now_wall.isoformat()})
    elif res == "failed":
        first = c.st.remind_fail_since.setdefault(a.alert_id, c.now_mono)
        if c.now_mono - first < PUSH_RETRY_SEC:
            a.reminded = False   # 다음 샘플에서 트래커가 다시 낸다(D15 조건도 다시 본다)
        else:
            logger.warning("v4 reminder 푸시 재시도 포기(10분) alert=%s", a.alert_id)
    # disabled: 기록 없이 끝(reminded=True 유지)


# ---------- Action 처리 ----------

def _chain_root(c: _Step, prev: ActiveEvent) -> str | None:
    ctx = c.st.ctx.get(prev.alert_id or "") or {}
    return ctx.get("chain_root") or prev.alert_id


def _insert_open(c: _Step, tracker: ElementTracker, act: Action,
                 prev: ActiveEvent | None, close: Action | None) -> bool:
    a = tracker.active
    assert a is not None and act.value is not None and act.threshold is not None
    ctx: dict[str, Any] = {
        "rule": "v4", "level": act.level, "value": act.value, "threshold": act.threshold,
        "diff": display_diff(act.kind, act.value, act.threshold), "peak_value": act.peak,
    }
    if a.silent and close is not None and prev is not None:          # D3 무음 완화
        ctx["deescalated_from"] = close.closes_alert_id
        ctx["chain_root"] = _chain_root(c, prev)
        ctx["chain_pushed_at"] = _iso_mono(c, a.chain_pushed_mono)
        ctx["chain_reminded"] = a.chain_reminded
    elif act.level == "danger" and not act.push and prev is not None:  # D14 무음 재경고
        ctx["push_suppressed"] = True
        ctx["chain_root"] = _chain_root(c, prev)
        ctx["chain_pushed_at"] = _iso_mono(c, a.pushed_mono)   # 체인 기준 추가 푸시(복원용)
        ctx["chain_reminded"] = a.reminded
    triggered = c.wall(act.at_mono)
    body = {
        "device_id": c.device_uuid,
        "kind": act.kind,
        "severity": _SEVERITY[act.level],
        "message": title(act.kind, act.level, act.value, act.threshold),
        "triggered_at": triggered.isoformat(),
        "context": ctx,
    }
    try:
        res = c.sb.table("alerts").insert(body).execute()
    except Exception:  # noqa: BLE001
        logger.exception("v4 alert INSERT 실패 — 다음 샘플 재판정 (device=%s kind=%s)",
                         c.device_uuid, act.kind)
        return False
    alert_id = ((res.data or [{}])[0] or {}).get("id")
    if not alert_id:
        logger.error("v4 alert INSERT 응답에 id 없음 (device=%s kind=%s)", c.device_uuid, act.kind)
        return False
    a.alert_id = alert_id
    a.started_wall = triggered
    c.st.ctx[alert_id] = ctx
    logger.info("v4 alert OPEN: device=%s kind=%s level=%s id=%s", c.device_uuid, act.kind, act.level, alert_id)
    return True


def _write_close(c: _Step, act: Action) -> None:
    aid = act.closes_alert_id
    if not aid:
        logger.warning("v4 close 인데 alert_id 없음 (device=%s kind=%s)", c.device_uuid, act.kind)
        return
    # peak_diff 기준은 그 행의 threshold(같은 행 안에서 diff 와 일관), 없으면 현재 기준
    thr = _num((c.st.ctx.get(aid) or {}).get("threshold"))
    if thr is None:
        thr = act.threshold
    patch: dict[str, Any] = {}
    if act.peak is not None and math.isfinite(act.peak):
        patch["peak_value"] = act.peak
        if thr is not None:
            patch["peak_diff"] = display_diff(act.kind, act.peak, thr)
    _write(c.sb, c.st, aid, row={"resolved_at": c.wall(act.at_mono).isoformat()}, ctx=patch)
    c.st.remind_fail_since.pop(aid, None)
    logger.info("v4 alert CLOSE: device=%s kind=%s id=%s", c.device_uuid, act.kind, aid)


def _apply(c: _Step, element: str, tracker: ElementTracker,
           acts: list[Action], prev: ActiveEvent | None) -> None:
    """트래커 Action 처리. open 을 먼저 INSERT 하고, 성공해야 같은 step 의 close 를 쓴다."""
    opens = [a for a in acts if a.op == "open"]
    closes = [a for a in acts if a.op == "close"]
    pending: _PendingPush | None = None
    if opens:
        act = opens[0]
        if not _insert_open(c, tracker, act, prev, closes[0] if closes else None):
            # 이전 활성으로 되돌림 — close 도 커밋 안 했으니 다음 샘플이 같은 전이를 다시 낸다.
            tracker.rollback_open()
            tracker.active = prev
            return
        if act.push:
            assert act.value is not None and tracker.active is not None
            pending = _PendingPush(
                alert_id=tracker.active.alert_id or "", kind=act.kind, level=act.level,
                title=title(act.kind, act.level, act.value, act.threshold),
                value=act.value, threshold=act.threshold, first_mono=c.now_mono,
            )
    for act in closes:
        _write_close(c, act)
    if pending is not None:
        _push_started(c, element, pending)
    for act in acts:
        if act.op == "remind":
            _remind(c, tracker, act)
        elif act.op == "peak" and tracker.active is not None and tracker.active.alert_id:
            _write(c.sb, c.st, tracker.active.alert_id, ctx={"peak_value": act.peak})


# ---------- v4 진입점 ----------

def _stop_unlinked(c: _Step) -> None:
    """기기 해제(D12): 활성 v4 행 종료 + 트래커 삭제. 이후 샘플은 평가하지 않는다."""
    if not c.st.unlinked:
        try:
            rows = select_active_v4_rows(c.sb, c.device_uuid)
        except Exception:  # noqa: BLE001
            logger.exception("해제 기기 v4 행 조회 실패 — 다음 샘플 재시도 (device=%s)", c.device_uuid)
            return
        for r in rows:
            _force_close(c.sb, c.st, r, c.now_wall)
        c.st.trackers = {e: ElementTracker(e) for e in _ELEMENTS}  # type: ignore[arg-type]
        c.st.push_retry.clear()
        c.st.unlinked = True
    _flush_dirty(c.sb, c.st)


def _sensor_fault_v4(sb: Any, st: _DeviceState, device_uuid: str, a_ok: Any) -> None:
    """sensor_fault INSERT/resolve 를 상태가 바뀔 때만(모르면 한 번 확인). 실패면 다음 샘플 재시도."""
    want = not a_ok
    if st.fault_active is want:
        return
    if want:
        ok = _insert_alert(sb, device_uuid, "sensor_fault", "warning", "DHT22-A 통신 실패",
                           {"sensor": "dht22_a"})
    else:
        ok = _resolve_alert(sb, device_uuid, "sensor_fault")
    if ok:
        st.fault_active = want


def _evaluate_v4(device_uuid: str, row: dict[str, Any], device_key: str | None) -> None:
    sb = get_supabase_client()
    meta = _device_meta(device_uuid)   # TTL 캐시. 실패면 None(해제 판정 못 함 → 평가는 계속)
    with _v4_lock:
        st = _states.setdefault(device_uuid, _DeviceState())
        c = _Step(sb=sb, st=st, device_uuid=device_uuid, device_key=device_key, meta=meta,
                  now_mono=_now_mono(), now_wall=_now_wall())
        if meta is not None and meta.get("unlinked_at"):
            _stop_unlinked(c)
            return
        if st.unlinked and meta is not None:
            # 같은 행 재페어링 — 해제 전 상태를 버리고 DB 복원부터 다시(다음 해제도 다시 닫게)
            st = c.st = _states[device_uuid] = _DeviceState()

        try:
            _sensor_fault_v4(sb, st, device_uuid, row.get("a_ok", False))
        except Exception:  # noqa: BLE001 — sensor_fault 조회 실패가 온습도 판정을 막지 않게
            logger.exception("sensor_fault 평가 실패 (device=%s)", device_uuid)
        _flush_dirty(sb, st)
        if not st.restored and not _restore(c):
            return
        th = _thresholds(c)
        if th is None:
            return   # 설정을 한 번도 못 읽음 — 이 샘플 보류(D8)
        _retry_pushes(c)

        t_val, h_val = _sample_values(row)
        for element, value in (("temp", t_val), ("humid", h_val)):
            tracker = st.trackers[element]
            prev = tracker.active
            acts = tracker.step(c.now_mono, value, th)
            if acts:
                _apply(c, element, tracker, acts, prev)


__all__ = [
    "HYST_HUMID_PCT",
    "HYST_TEMP_C",
    "LEGACY_BRIDGE_KINDS",
    "V4_KINDS",
    "evaluate_telemetry",
    "forced_close_context",
    "reset_settings_cache",
    "reset_state",
    "select_active_v4_rows",
    "v4_enabled",
]
