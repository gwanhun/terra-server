"""
습기 사이클 DB·푸시 연결 — Stage O (specs/stage-o-humid-cycle-v4.md Task 4).

`alerts._evaluate_v4` 가 습도 샘플마다 `step()` 을 부른다(`HUMID_CYCLES_ENABLED` + `HUMID_CYCLES_SINCE`).
판정은 `backend/humid_cycle.py`(순수), 이 모듈은 복원·humid_cycles 기록·환기 지연/분무 공백 알림·푸시만 한다.

## 기록 규칙

- 샘플 시각 = `row.ts`(기기 시계)가 서버 wall 과 ±10분 안이면 그것, 아니면 wall. 재연결 뒤 몰려 온
  샘플도 원래 간격으로 본다.
- 트래커가 낸 op 은 기기별 **대기열**에 넣고 순서대로 쓴다. 쓰기가 실패하면 그 op 에서 멈추고 다음
  샘플에서 이어 쓴다 — 새 사이클 `open` 이 저장되기 전엔 직전 사이클 종료(`not_reached`·알림 종료)를
  쓰지 않는다. `open` 은 `UNIQUE(device_id, started_at)` upsert 라 응답 유실 재시도에도 행이 하나다.
- 알림(`env_vent_delay`·`env_mist_gap`)은 열기 직전 해제를 재확인하고(`confirm_linked`), 같은 사이클의
  활성 행이 이미 있으면 그 행을 쓴다(INSERT 응답 유실 대비). started 푸시 1회, 성공하면
  `context.pushed_at`, 실패(`failed`)면 10분 동안 샘플마다 재시도(재시도 직전에도 해제 재확인).
- 알림 종료 시각 = max(종료 op 시각, 알림 triggered_at), `context.closed_reason` = dry/next_event/stale/…

## 복원 (기기 첫 호출)

- `HUMID_CYCLES_SINCE` 이후 시작했고 `ended_by` 가 없는 최신 사이클 1행 + 활성 Stage O 알림.
- `*_fired` = 사이클 행의 `*_alert_id` 가 있음. 사이클이 이미 도달·종료인데 열린 환기 지연, 다른 사이클을
  가리키는 열린 알림은 닫는다. `pushed_at` 없는 10분 안 알림은 푸시 재시도 목록에.
- 재시작 공백: 현재 샘플 ts 직전의 유효 습도 telemetry 행 ts 를 트래커의 마지막 샘플 시각으로 넘긴다
  (60초 넘으면 트래커가 offline 으로 센다). 그런 행이 없으면(보존 7일 밖) 사이클 `updated_at`.
- 조회가 실패하면 그 샘플은 보류(다음 샘플 재시도).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend import push_events
from backend.humid_cycle import (
    REACHED,
    CycleOp,
    CycleState,
    HumidCycleTracker,
    humid_cycles_since,
    max_humid,
)

logger = logging.getLogger(__name__)

CYCLE_KINDS = ("env_vent_delay", "env_mist_gap")
TITLES = {"env_vent_delay": "[환기 지연] 분무 후 건기 미도달", "env_mist_gap": "[건조 주의] 분무 필요"}
_ALERT_ID_COL = {"env_vent_delay": "vent_delay_alert_id", "env_mist_gap": "mist_gap_alert_id"}
CLOCK_SKEW_SEC = 600
RETRY_SEC = 600
QUEUE_MAX = 200          # DB 장애가 길어 대기열이 이만큼 쌓이면 버리고 DB 에서 다시 복원
_UTC = timezone.utc


@dataclass
class CycleDeviceState:
    tracker: HumidCycleTracker | None = None
    restored: bool = False
    queue: list[CycleOp] = field(default_factory=list)                 # 아직 못 쓴 op(순서대로)
    alert_ctx: dict[str, dict[str, Any]] = field(default_factory=dict)  # alert_id → context
    alert_at: dict[str, str] = field(default_factory=dict)              # alert_id → triggered_at
    push_retry: dict[str, tuple[str, float | None, datetime]] = field(default_factory=dict)


# ---------- 작은 도우미 ----------

def _iso(dt: datetime) -> str:
    return dt.astimezone(_UTC).isoformat()


def _parse(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_UTC)


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sample_time(row: dict[str, Any], now_wall: datetime) -> datetime:
    ts = _parse(row.get("ts"))
    if ts is None or abs((ts - now_wall).total_seconds()) > CLOCK_SKEW_SEC:
        return now_wall
    return ts


def _cycle_patch(c: CycleState, now: datetime, **extra: Any) -> dict[str, Any]:
    patch = {
        "detected_at": _iso(c.detected_at), "max_until": _iso(c.max_until),
        "max_raw": round(c.peak, 1),
        "max_humid": max_humid(c.peak) if c.dry_target is not None else None,
        "dry_target": c.dry_target,
        "dry_reached_at": _iso(c.dry_reached_at) if c.dry_reached_at else None,
        "status": c.status, "offline_sec": int(round(c.offline_sec)),
        "merged_count": c.merged_count, "updated_at": _iso(now),
    }
    patch.update(extra)
    return patch


# ---------- 공개: 해제·롤백 경로 ----------

def select_active_cycle_alerts(sb: Any, device_uuid: str | None = None) -> list[dict[str, Any]]:
    q = (sb.table("alerts").select("id, device_id, kind, triggered_at, context")
         .is_("resolved_at", "null").in_("kind", list(CYCLE_KINDS)))
    if device_uuid is not None:
        q = q.eq("device_id", device_uuid)
    rows = q.execute().data or []
    return [r for r in rows if isinstance(r.get("context"), dict) and r["context"].get("rule") == "v4"]


def close_cycle_alert(sb: Any, row: dict[str, Any], resolved_at: str, reason: str) -> None:
    trig = row.get("triggered_at")
    if isinstance(trig, str) and _parse(trig) and _parse(resolved_at) and _parse(trig) > _parse(resolved_at):  # type: ignore[operator]
        resolved_at = trig
    ctx = {**(row.get("context") or {}), "closed_reason": reason}
    sb.table("alerts").update({"resolved_at": resolved_at, "context": ctx}).eq("id", row["id"]).execute()


def end_latest_cycle(sb: Any, device_uuid: str, ended_by: str, at: str) -> None:
    """기기 최신 사이클을 끝낸다(해제·스위치 끔). 이후 복원·분무 공백 타이머에서 빠진다."""
    rows = (sb.table("humid_cycles").select("id, status, closed_at").eq("device_id", device_uuid)
            .is_("ended_by", "null").order("started_at", desc=True).limit(1).execute().data or [])
    if not rows:
        return
    r = rows[0]
    patch: dict[str, Any] = {"ended_by": ended_by, "updated_at": at}
    if r.get("closed_at") is None:
        patch["closed_at"] = at
    if r.get("status") == "open":
        patch["status"] = "not_reached"
    sb.table("humid_cycles").update(patch).eq("id", r["id"]).execute()


def close_device(sb: Any, device_uuid: str, at: str, ended_by: str) -> None:
    """열린 Stage O 알림 종료 + 최신 사이클 끝냄. 예외는 호출측이 처리."""
    for r in select_active_cycle_alerts(sb, device_uuid):
        close_cycle_alert(sb, r, at, ended_by)
    end_latest_cycle(sb, device_uuid, ended_by, at)


def on_unlinked(sb: Any, st: CycleDeviceState, device_uuid: str, now_wall: datetime) -> None:
    """브리지가 해제를 확인함 — 알림·사이클 종료 후 상태 비움(재페어링은 새로 복원)."""
    close_device(sb, device_uuid, _iso(now_wall), "unlinked")
    st.tracker = None
    st.restored = False
    st.queue.clear()
    st.push_retry.clear()


# ---------- 복원 ----------

def _state_from_row(r: dict[str, Any], active: dict[str, dict[str, Any]]) -> CycleState:
    started = _parse(r["started_at"])
    assert started is not None
    vent_id, mist_id = r.get("vent_delay_alert_id"), r.get("mist_gap_alert_id")
    return CycleState(
        cycle_id=r["id"], started_at=started, start_humid=_num(r.get("start_humid")) or 0.0,
        detected_at=_parse(r.get("detected_at")) or started,
        peak=_num(r.get("max_raw")) or 0.0,
        max_until=_parse(r.get("max_until")) or started,
        dry_target=_num(r.get("dry_target")),
        dry_reached_at=_parse(r.get("dry_reached_at")),
        status=r.get("status") or "open",
        offline_sec=_num(r.get("offline_sec")) or 0.0,
        merged_count=int(_num(r.get("merged_count")) or 0),
        vent_delay_alert_id=vent_id, vent_delay_fired=vent_id is not None,
        vent_delay_closed=vent_id is not None and vent_id not in active,
        mist_gap_alert_id=mist_id, mist_gap_fired=mist_id is not None,
        mist_gap_closed=mist_id is not None and mist_id not in active,
    )


def _leftover_close(r: dict[str, Any], cycle: CycleState | None, now: datetime) -> tuple[str, str]:
    """복원 때 남은 알림의 종료 (시각, 사유). 쓰기 실패 등으로 못 닫힌 알림이라 그때 시각으로 메운다."""
    if cycle is not None and (r.get("context") or {}).get("cycle_id") == cycle.cycle_id:
        if r["kind"] == "env_vent_delay" and cycle.dry_reached_at is not None:
            return _iso(cycle.dry_reached_at), "dry"
        return _iso(now), "restore"
    if cycle is not None:
        return _iso(cycle.started_at), "next_event"   # 이전 사이클 알림 — 이 사이클이 열리며 끝났어야 함
    return _iso(now), "restore"


def _restore(c: Any, st: CycleDeviceState, at: datetime) -> bool:
    since = humid_cycles_since()
    assert since is not None
    try:
        rows = (c.sb.table("humid_cycles").select("*").eq("device_id", c.device_uuid)
                .gte("started_at", _iso(since)).is_("ended_by", "null")
                .order("started_at", desc=True).limit(1).execute().data or [])
        active_rows = select_active_cycle_alerts(c.sb, c.device_uuid)
        prev_ts: datetime | None = None
        if rows:
            prev = (c.sb.table("telemetry").select("ts").eq("device_id", c.device_uuid)
                    .eq("a_ok", True).not_.is_("h_a", "null").lt("ts", _iso(at))
                    .order("ts", desc=True).limit(1).execute().data or [])
            prev_ts = _parse(prev[0]["ts"]) if prev else None
    except Exception:  # noqa: BLE001
        logger.exception("습기 사이클 복원 조회 실패 — 다음 샘플 재시도 (device=%s)", c.device_uuid)
        return False

    active = {r["id"]: r for r in active_rows}
    cycle = _state_from_row(rows[0], active) if rows else None
    keep: set[str] = set()
    if cycle is not None:
        if cycle.vent_delay_alert_id in active and cycle.status == "open":
            keep.add(cycle.vent_delay_alert_id)  # type: ignore[arg-type]
        if cycle.mist_gap_alert_id in active and cycle.status in REACHED:
            keep.add(cycle.mist_gap_alert_id)  # type: ignore[arg-type]
    for aid, r in active.items():
        if aid in keep:
            st.alert_ctx[aid] = dict(r.get("context") or {})
            st.alert_at[aid] = r.get("triggered_at") or ""
            pushed = (r.get("context") or {}).get("pushed_at")
            trig = _parse(r.get("triggered_at"))
            if not pushed and trig is not None and (c.now_wall - trig).total_seconds() < RETRY_SEC:
                st.push_retry[aid] = (r["kind"], _num((r.get("context") or {}).get("value")), trig)
            continue
        # 도달·종료된 사이클에 남은 알림, 다른 사이클의 알림 → 실제로 끝났어야 할 시각으로 닫는다
        when, reason = _leftover_close(r, cycle, c.now_wall)
        try:
            close_cycle_alert(c.sb, r, when, reason)
        except Exception:  # noqa: BLE001
            logger.exception("남은 습기 알림 종료 실패 — 다음 복원 때 재시도 (alert=%s)", aid)
            return False
        if cycle is not None:
            if aid == cycle.vent_delay_alert_id:
                cycle.vent_delay_closed = True
            if aid == cycle.mist_gap_alert_id:
                cycle.mist_gap_closed = True

    last = prev_ts
    if cycle is not None and last is None:
        last = _parse(rows[0].get("updated_at"))
    st.tracker = HumidCycleTracker(cycle=cycle, last_sample_at=last if cycle is not None else None)
    st.restored = True
    return True


# ---------- op 쓰기 ----------

def _write_cycle(c: Any, op: CycleOp) -> None:
    cy = op.cycle
    now = c.now_wall
    if op.op == "open":
        body = {"device_id": c.device_uuid, "started_at": _iso(cy.started_at),
                "start_humid": cy.start_humid, "closed_at": None, "ended_by": None,
                "vent_delay_alert_id": None, "mist_gap_alert_id": None,
                **_cycle_patch(cy, now)}
        res = c.sb.table("humid_cycles").upsert(body, on_conflict="device_id,started_at").execute()
        cy.cycle_id = ((res.data or [{}])[0] or {}).get("id")
        if not cy.cycle_id:
            raise RuntimeError("humid_cycles upsert 응답에 id 없음")
        logger.info("습기 이벤트 OPEN: device=%s start=%s humid=%.1f id=%s",
                    c.device_uuid, body["started_at"], cy.start_humid, cy.cycle_id)
        return
    extra: dict[str, Any] = {}
    if op.op == "not_reached":
        extra = {"closed_at": _iso(op.at), "ended_by": "next_event"}
    # 해제 API·롤백 스크립트가 끝낸 행(ended_by)은 브리지가 되돌려 쓰지 않는다(캐시상 아직 연결됨일 때)
    c.sb.table("humid_cycles").update(_cycle_patch(cy, now, **extra)) \
        .eq("id", cy.cycle_id).is_("ended_by", "null").execute()


def _open_alert(c: Any, st: CycleDeviceState, op: CycleOp, confirm_linked: Callable[[], bool]) -> bool:
    """False = 해제 확인(대기열 중단)."""
    kind = "env_vent_delay" if op.op == "vent_delay" else "env_mist_gap"
    cy = op.cycle
    if not confirm_linked():
        return False
    existing = [r for r in select_active_cycle_alerts(c.sb, c.device_uuid)
                if r["kind"] == kind and (r.get("context") or {}).get("cycle_id") == cy.cycle_id]
    if existing:
        row = existing[0]
        alert_id = row["id"]
        ctx = dict(row.get("context") or {})
        triggered = row.get("triggered_at") or _iso(op.at)
    else:
        ctx = {"rule": "v4", "level": "caution", "cycle_id": cy.cycle_id,
               "cycle_started_at": _iso(cy.started_at), "value": op.value,
               "max_humid": max_humid(cy.peak) if cy.dry_target is not None else None,
               "dry_target": cy.dry_target}
        triggered = _iso(op.at)
        res = c.sb.table("alerts").insert({
            "device_id": c.device_uuid, "kind": kind, "severity": "warning", "message": TITLES[kind],
            "triggered_at": triggered, "context": ctx,
        }).execute()
        alert_id = ((res.data or [{}])[0] or {}).get("id")
        if not alert_id:
            raise RuntimeError("alerts INSERT 응답에 id 없음")
        logger.info("습기 알림 OPEN: device=%s kind=%s id=%s", c.device_uuid, kind, alert_id)
    st.alert_ctx[alert_id] = ctx
    st.alert_at[alert_id] = triggered
    if kind == "env_vent_delay":
        cy.vent_delay_alert_id = alert_id
    else:
        cy.mist_gap_alert_id = alert_id
    c.sb.table("humid_cycles").update({_ALERT_ID_COL[kind]: alert_id, "updated_at": _iso(c.now_wall)}) \
        .eq("id", cy.cycle_id).execute()
    if not ctx.get("pushed_at"):
        _push(c, st, alert_id, kind, op.value, first=c.now_wall)
    return True


def _close_alert(c: Any, st: CycleDeviceState, op: CycleOp) -> None:
    kind = "env_vent_delay" if op.op == "vent_delay_close" else "env_mist_gap"
    alert_id = op.cycle.vent_delay_alert_id if kind == "env_vent_delay" else op.cycle.mist_gap_alert_id
    if alert_id is None:
        return   # 알림을 못 열었던 사이클
    row = {"id": alert_id, "triggered_at": st.alert_at.get(alert_id),
           "context": st.alert_ctx.get(alert_id) or {"rule": "v4", "level": "caution"}}
    close_cycle_alert(c.sb, row, _iso(op.at), op.reason or "next_event")
    st.alert_ctx.pop(alert_id, None)
    st.alert_at.pop(alert_id, None)
    st.push_retry.pop(alert_id, None)


def _push(c: Any, st: CycleDeviceState, alert_id: str, kind: str, value: float | None,
          first: datetime) -> None:
    if not push_events.env_alerts_push_enabled():
        return
    res = "failed"
    if c.meta is not None:
        ev = push_events.build_env_alert_event(
            alert_id=alert_id, kind=kind, level="caution", phase="started", title=TITLES[kind],
            value=value if value is not None else 0.0, threshold=None,
            device_id=c.device_uuid, device_key=c.device_key, device_meta=c.meta,
        )
        res = "disabled" if ev is None else push_events.enqueue_env_alert(ev)
    if res in ("queued", "duplicate"):
        st.push_retry.pop(alert_id, None)
        ctx = {**st.alert_ctx.get(alert_id, {}), "pushed_at": _iso(c.now_wall)}
        st.alert_ctx[alert_id] = ctx
        try:
            c.sb.table("alerts").update({"context": ctx}).eq("id", alert_id).execute()
        except Exception:  # noqa: BLE001 — 기록 실패는 다음 복원 때 재적재(event_id 멱등)
            logger.exception("습기 알림 pushed_at 기록 실패 (alert=%s)", alert_id)
    elif res == "failed" and (c.now_wall - first).total_seconds() < RETRY_SEC:
        st.push_retry[alert_id] = (kind, value, first)
    else:
        if res == "failed":
            logger.warning("습기 알림 푸시 재시도 포기(10분) alert=%s", alert_id)
        st.push_retry.pop(alert_id, None)


def _drain(c: Any, st: CycleDeviceState, confirm_linked: Callable[[], bool]) -> bool:
    """대기열을 순서대로 쓴다. 실패하면 그 op 에서 멈춤. False = 해제 확인."""
    while st.queue:
        op = st.queue[0]
        try:
            if op.op in ("vent_delay", "mist_gap"):
                if not _open_alert(c, st, op, confirm_linked):
                    return False
            elif op.op in ("vent_delay_close", "mist_gap_close"):
                _close_alert(c, st, op)
            elif op.op == "open" and not confirm_linked():
                return False   # 해제된 기기에 새 사이클을 만들지 않는다(브리지 캐시가 늦을 때)
            else:
                _write_cycle(c, op)
        except Exception:  # noqa: BLE001 — supabase-py 예외 타입 넓음
            logger.exception("습기 사이클 쓰기 실패 — 다음 샘플 재시도 (device=%s op=%s)",
                             c.device_uuid, op.op)
            return True
        st.queue.pop(0)
    return True


# ---------- 진입점 ----------

def step(c: Any, st: CycleDeviceState, row: dict[str, Any], humid: float | None,
         confirm_linked: Callable[[], bool]) -> None:
    """습도 샘플 1건. `c` 는 alerts._Step(sb·device_uuid·device_key·meta·now_wall)."""
    at = sample_time(row, c.now_wall)
    if not st.restored and not _restore(c, st, at):
        return
    assert st.tracker is not None
    if st.push_retry:
        if not confirm_linked():
            return
        for aid, (kind, value, first) in list(st.push_retry.items()):
            _push(c, st, aid, kind, value, first)
    st.queue.extend(st.tracker.step(at, humid))
    if len(st.queue) > QUEUE_MAX:
        logger.error("습기 사이클 대기열 %d개 — 버리고 DB 에서 다시 복원 (device=%s)",
                     len(st.queue), c.device_uuid)
        st.queue.clear()
        st.restored = False
        return
    _drain(c, st, confirm_linked)


__all__ = [
    "CYCLE_KINDS",
    "TITLES",
    "CycleDeviceState",
    "close_cycle_alert",
    "close_device",
    "end_latest_cycle",
    "on_unlinked",
    "sample_time",
    "select_active_cycle_alerts",
    "step",
]
