"""
자동 환기·자동 분무 DB 글루 — Stage R (specs/stage-r-auto-env.md Task 3).

판정은 `backend/auto_env.py`(순수 상태 기계)가 하고, 이 모듈은 그 주변의 DB·로그만 맡는다.
`alerts._evaluate_v4` 가 샘플마다 `step()` 을, `alerts.on_command_ack` 가 ACK 마다 `handle_ack()` 를
`_v4_lock` 안에서 부른다. 상태(`AutoDeviceState`)는 `alerts._DeviceState.auto` 한 곳에 있다.

## 스위치 (호출마다 env 를 읽는다 — `humid_cycles_enabled` 와 같은 방식)

| env | 기본 | 뜻 |
|-----|------|----|
| `AUTO_ENV_ENABLED` | 꺼짐 | true/1/yes/on 일 때만 판정(게이트는 alerts 쪽) |
| `AUTO_ENV_DRY_RUN` | **켜짐** | 0/false/no/off 만 끔. 켜져 있으면 INSERT 대신 `auto-env intent` 로그 |
| `AUTO_ENV_DEVICES` | 비움 = 전체 | 쉼표 구분 기기 UUID 허용목록 |

## 샘플 1건 (`step`)

1. 토글 둘 다 꺼짐 + 자동 소유 없음 → 트래커만 굴리고 끝(DB 0회). 소유 중이면 꺼짐이어도 아래로
   가서 `auto_vent_disabled` 를 낸다(R10).
2. 첫 샘플: 복원(R11) — `devices.auto_env_state` + 최근 1시간 팬 명령(마지막 작성자) + 오늘 분무 명령.
   조회 실패면 이 샘플 보류.
3. 예약 구간(R8): 기기별 60초 캐시. 조회 실패면 직전 목록을 쓰고 30초 뒤 재조회(목록이 한 번도
   없으면 이 샘플 보류).
4. 트래커 판정 → 명령마다: 팬 명령은 INSERT 직전 예약을 캐시 없이 다시 읽고 구간이면 넣지 않는다.
   섀도면 로그 + 즉시 ACK ok + 모의 팬. 아니면 `insert_pending_command(source='auto')`.
5. 상태 쓰기(R12): `updated_at` 을 뺀 내용이 바뀔 때만 `devices.auto_env_state` UPDATE, 실패하면
   60초 뒤 재시도. 섀도 모드에선 쓰지 않는다(앱이 가짜 상태를 보면 안 됨).

## ACK (`handle_ack`)

- `source='auto'` → `on_own_ack(action, result=='ok')`.
- 그 밖의 팬 명령 ACK ok → `on_other_ack` + 그 기기의 pending 자동 팬 명령을 `expired/superseded` 로(R7).
- source 무관 `mist` ACK ok → `on_mist_ok`(R9 분무 간격 2시간. 환기 판정엔 안 씀 — r3 에서 분무 뒤 보호 삭제).
- 이 브리지에서 아직 복원하지 않은 기기(`restored=False`)는 무시 — 복원이 DB 에서 같은 사실을 읽는다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.auto_env import (
    CAUSES,
    KST,
    MIST_MIN_INTERVAL_SEC,
    STATE_RETRY_SEC,
    VENT_DURATION_MS,
    AutoCmd,
    AutoEnvTracker,
    AutoInput,
)
from backend.command_service import insert_pending_command
from backend.daily_analysis import num as _num
from backend.daily_analysis import parse_ts as _parse
from backend.env_rules import Thresholds
from backend.scheduling import compute_prev_run, parse_time_of_day

logger = logging.getLogger(__name__)

AUTO_SOURCE = "auto"
FAN_ACTIONS = ("fan_on", "fan_off", "fan_toggle")
SPAN_ACTIONS = ("fan_on", "fan_off")
SCHEDULE_TTL_SEC = 60.0          # R8 예약 목록 캐시
SCHEDULE_RETRY_SEC = 30.0        # 예약 조회 실패 뒤 재조회까지
RESTORE_FAN_WINDOW_SEC = 3600    # R11 마지막 작성자 조회 창(= 자동 fan_on 타이머)
SIM_FAN_SEC = VENT_DURATION_MS / 1000

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_UTC = timezone.utc
# 이 상태면 그 명령은 팬에 닿지 않았다 — 마지막 작성자 판정에서 건너뛴다
_NOT_APPLIED = ("expired", "rejected")


# ---------- 스위치 ----------

def auto_env_enabled() -> bool:
    """`AUTO_ENV_ENABLED` — 기본 꺼짐."""
    return (os.getenv("AUTO_ENV_ENABLED") or "").strip().lower() in _TRUE


def dry_run() -> bool:
    """`AUTO_ENV_DRY_RUN` — 기본 **켜짐**(섀도). 0/false/no/off 만 끈다(오타는 안전한 쪽)."""
    return (os.getenv("AUTO_ENV_DRY_RUN") or "").strip().lower() not in _FALSE


def device_allowed(device_uuid: str) -> bool:
    """`AUTO_ENV_DEVICES` 허용목록(쉼표 구분). 비면 전체."""
    raw = (os.getenv("AUTO_ENV_DEVICES") or "").strip()
    if not raw:
        return True
    return device_uuid in {x.strip() for x in raw.split(",") if x.strip()}


# ---------- 상태 ----------

@dataclass
class AutoDeviceState:
    """기기 하나의 자동 경로 상태. `alerts._DeviceState.auto` 에 산다(재페어링·스위치 끔 때 새로)."""

    tracker: AutoEnvTracker = field(default_factory=AutoEnvTracker)
    restored: bool = False
    spans: list[dict[str, Any]] | None = None      # 예약 행 캐시(R8). None = 한 번도 못 읽음
    spans_expires: float = 0.0
    last_state: dict[str, Any] | None = None       # 마지막으로 쓴 auto_env_state(updated_at 제외)
    state_retry_at: float = 0.0
    sim_until: float | None = None                 # 섀도 모의 팬: 이 mono 전까지 'ON'(R13)
    hold_prev: bool = False                        # 직전 샘플의 예약 구간 여부(켜짐 전이 감지)


# ---------- 작은 도우미 ----------

def _iso(dt: datetime) -> str:
    return dt.astimezone(_UTC).isoformat()


def _to_mono(c: Any, dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return c.now_mono - (c.now_wall - dt).total_seconds()


def _kst_midnight(wall: datetime) -> datetime:
    d = wall.astimezone(KST).date()
    return datetime.combine(d, datetime.min.time(), KST)


# ---------- 예약 구간(R8) ----------

def _load_spans(c: Any) -> list[dict[str, Any]]:
    res = (
        c.sb.table("schedules")
        .select("id, action, kind, time_of_day, days_of_week, enabled, pair_id")
        .eq("device_id", c.device_uuid)
        .eq("enabled", True)
        .in_("action", list(SPAN_ACTIONS))
        .not_.is_("pair_id", "null")
        .execute()
    )
    return [r for r in (res.data or []) if isinstance(r, dict)]


def span_active(rows: list[dict[str, Any]], now_wall: datetime) -> bool:
    """구간 예약 켜짐 중인가. on·off 두 행이 다 enabled 인 pair 만, 늦은 쪽이 fan_on 이면 켜짐. 합집합."""
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        if r.get("enabled") is not True or not r.get("pair_id") or r.get("action") not in SPAN_ACTIONS:
            continue
        pairs.setdefault(str(r["pair_id"]), {})[r["action"]] = r
    now = now_wall.astimezone(_UTC)
    for pid, by_action in pairs.items():
        if len(by_action) != 2:
            continue    # 짝이 없거나 한쪽이 꺼짐 → 무효
        try:
            prev = {
                a: compute_prev_run(now, r["kind"], parse_time_of_day(r["time_of_day"]), r.get("days_of_week"))
                for a, r in by_action.items()
            }
        except (ValueError, RuntimeError, KeyError, TypeError):
            logger.warning("auto-env: 예약 pair %s 시각 계산 실패 — 건너뜀", pid, exc_info=True)
            continue
        if prev["fan_on"] > prev["fan_off"]:
            return True
    return False


def _schedule_hold(c: Any, st: AutoDeviceState) -> bool | None:
    """캐시된 예약으로 구간 여부. 목록을 한 번도 못 읽었으면 None(이 샘플 보류)."""
    if st.spans is None or c.now_mono >= st.spans_expires:
        try:
            st.spans = _load_spans(c)
            st.spans_expires = c.now_mono + SCHEDULE_TTL_SEC
        except Exception as exc:  # noqa: BLE001 — supabase-py 예외 타입 넓음
            logger.warning("auto-env: 예약 조회 실패 (device=%s): %s", c.device_uuid, exc)
            st.spans_expires = c.now_mono + SCHEDULE_RETRY_SEC
            if st.spans is None:
                return None
    return span_active(st.spans, c.now_wall)


# ---------- 복원(R11) ----------

def _restore(c: Any, st: AutoDeviceState) -> bool:
    """재시작 복원. 조회 하나라도 실패하면 False(이 샘플 보류, 다음 샘플 재시도)."""
    now_wall = c.now_wall
    midnight = _kst_midnight(now_wall)
    mist_since = min(midnight, now_wall - timedelta(seconds=MIST_MIN_INTERVAL_SEC))
    try:
        dev = (c.sb.table("devices").select("auto_env_state").eq("id", c.device_uuid)
               .limit(1).execute().data or [])
        fan_rows = (
            c.sb.table("commands")
            .select("action, status, result, source, reason, issued_at, acked_at")
            .eq("device_id", c.device_uuid)
            .in_("action", list(FAN_ACTIONS))
            .gte("issued_at", _iso(now_wall - timedelta(seconds=RESTORE_FAN_WINDOW_SEC)))
            .order("issued_at", desc=True)
            .execute().data or []
        )
        mist_rows = (
            c.sb.table("commands")
            .select("status, result, source, issued_at, acked_at")
            .eq("device_id", c.device_uuid)
            .eq("action", "mist")
            .gte("issued_at", _iso(mist_since))
            .order("issued_at", desc=True)
            .execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-env: 복원 조회 실패 — 이 샘플 보류 (device=%s): %s", c.device_uuid, exc)
        return False

    state = dev[0].get("auto_env_state") if dev and isinstance(dev[0], dict) else None
    state = state if isinstance(state, dict) else {}
    vent = state.get("vent") if isinstance(state.get("vent"), dict) else {}
    mist = state.get("mist") if isinstance(state.get("mist"), dict) else {}
    today = now_wall.astimezone(KST).date()
    try:
        state_day: date | None = date.fromisoformat(state["day"]) if isinstance(state.get("day"), str) else None
    except ValueError:
        state_day = None
    same_day = state_day == today

    # ① 상태 열
    runtime = float(_num(vent.get("runtime_today_sec")) or 0.0) if same_day else 0.0
    state_mist_today = int(_num(mist.get("today_count")) or 0) if same_day else 0
    state_mist_last = _parse(mist.get("last_at"))

    # ② 분무: source 무관 ACK ok 마지막 시각 + 오늘(KST) 자동 시도 수
    ok_times = [
        _parse(r.get("acked_at")) or _parse(r.get("issued_at"))
        for r in mist_rows if r.get("status") == "acked" and r.get("result") == "ok"
    ]
    query_mist_last = max((t for t in ok_times if t is not None), default=None)
    auto_today = sum(
        1 for r in mist_rows
        if r.get("source") == AUTO_SOURCE and (_parse(r.get("issued_at")) or midnight) >= midnight
    )
    mist_last = max((t for t in (state_mist_last, query_mist_last) if t is not None), default=None)
    mist_today = max(state_mist_today, auto_today)

    # ② 팬: 마지막 작성자가 자동 fan_on + ACK ok 일 때만 소유
    owned_at: datetime | None = None
    pending_on: datetime | None = None
    causes: list[str] = []
    # 상태 무관 가장 최근 자동 환기 명령 — 재시작해도 300초 간격(REFIRE_MIN_SEC)을 지키려고(리뷰 #5)
    last_cmd = next((_parse(r.get("issued_at")) for r in fan_rows if r.get("source") == AUTO_SOURCE), None)
    for r in fan_rows:      # issued_at 내림차순
        status = r.get("status")
        if status in _NOT_APPLIED or (status == "acked" and r.get("result") != "ok"):
            continue        # 팬에 닿지 않은 명령은 작성자가 아님
        if r.get("source") == AUTO_SOURCE and r.get("action") == "fan_on":
            if status == "acked":
                owned_at = _parse(r.get("acked_at")) or _parse(r.get("issued_at"))
                reason = r.get("reason") or ""
                if isinstance(reason, str) and reason.startswith("auto_vent:"):
                    causes = [x for x in reason.split(":", 1)[1].split("+") if x in CAUSES]
            elif status in ("pending", "sent"):
                pending_on = _parse(r.get("issued_at"))   # 아직 ACK 전 — 늦은 ACK ok 면 인수(리뷰 #2)
        break               # 그 밖(사용자·예약·자동 fan_off·no_ack)이 마지막이면 소유 없음

    run_since = None
    if owned_at is not None and vent.get("active") is True:
        since = _parse(vent.get("since"))
        if since is not None and since <= owned_at:
            run_since = since

    st.tracker.restore(
        mono=c.now_mono, wall=now_wall, day=None,
        paused_until=_to_mono(c, _parse(vent.get("paused_until"))),
        last_end=_to_mono(c, _parse(vent.get("last_end_at"))),
        runtime_today_sec=runtime, mist_today=mist_today,
        mist_last=_to_mono(c, mist_last),
        owned_on=_to_mono(c, owned_at), run_since=_to_mono(c, run_since),
        causes=causes, last_cmd=_to_mono(c, last_cmd), pending_on=_to_mono(c, pending_on),
    )
    st.restored = True
    st.sim_until = None
    # DB 에 이미 있는 내용과 같으면 다시 쓰지 않게(R12)
    st.last_state = {k: v for k, v in state.items() if k != "updated_at"} if state else None
    logger.info("auto-env: 복원 (device=%s owned=%s mist_today=%d)", c.device_uuid,
                owned_at is not None, mist_today)
    return True


# ---------- 명령 ----------

def _issue(c: Any, st: AutoDeviceState, cmd: AutoCmd) -> None:
    tr = st.tracker
    now = c.now_mono
    if cmd.action in SPAN_ACTIONS:
        # R8: INSERT 직전 캐시 없이 다시 읽는다
        try:
            rows = _load_spans(c)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-env: INSERT 전 예약 재조회 실패 — 30초 뒤 재시도 (device=%s): %s",
                           c.device_uuid, exc)
            tr.insert_failed(cmd, now)
            return
        st.spans, st.spans_expires = rows, now + SCHEDULE_TTL_SEC
        if span_active(rows, c.now_wall):
            logger.info("auto-env: 예약 구간 켜짐 — %s 보류 (device=%s)", cmd.action, c.device_uuid)
            return      # 다음 샘플은 캐시의 hold 로 소유를 놓는다

    if dry_run():
        logger.info("auto-env intent device=%s action=%s reason=%s", c.device_uuid, cmd.action, cmd.reason)
        tr.issued(cmd, now)
        tr.on_own_ack(cmd.action, True, now)
        if cmd.action == "fan_on":
            st.sim_until = now + SIM_FAN_SEC
        elif cmd.action == "fan_off":
            st.sim_until = None
        return

    owner = (c.meta or {}).get("owner_id")
    try:
        row = insert_pending_command(
            c.sb, device_uuid=c.device_uuid, action=cmd.action, payload=cmd.payload,
            issued_by=owner, source=AUTO_SOURCE, reason=cmd.reason,
        )
    except Exception:  # noqa: BLE001
        logger.exception("auto-env: 명령 INSERT 실패 (device=%s action=%s)", c.device_uuid, cmd.action)
        row = None
    if row is None:
        tr.insert_failed(cmd, now)
        return
    tr.issued(cmd, now)
    logger.info("auto-env: %s 적재 (device=%s reason=%s command=%s)",
                cmd.action, c.device_uuid, cmd.reason, row.get("id"))


def _supersede_pending(c: Any) -> None:
    """R7: 그 기기의 아직 안 나간 자동 팬 명령 무효화(조건부 UPDATE — 이미 나간 건 안 건드림)."""
    try:
        (c.sb.table("commands")
         .update({"status": "expired", "result": "superseded"})
         .eq("device_id", c.device_uuid)
         .eq("source", AUTO_SOURCE)
         .eq("status", "pending")
         .in_("action", list(FAN_ACTIONS))
         .execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-env: pending 자동 팬 명령 무효화 실패 (device=%s): %s", c.device_uuid, exc)


# ---------- 상태 쓰기(R12) ----------

def _sync_state(c: Any, st: AutoDeviceState) -> None:
    if dry_run():
        return
    full = st.tracker.state_json(c.now_wall)
    body = {k: v for k, v in full.items() if k != "updated_at"}
    if body == st.last_state or c.now_mono < st.state_retry_at:
        return
    try:
        c.sb.table("devices").update({"auto_env_state": full}).eq("id", c.device_uuid).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-env: auto_env_state 쓰기 실패 — %ds 뒤 재시도 (device=%s): %s",
                       STATE_RETRY_SEC, c.device_uuid, exc)
        st.state_retry_at = c.now_mono + STATE_RETRY_SEC
        return
    st.last_state = body


# ---------- 공개 ----------

def step(c: Any, st: AutoDeviceState, row: dict[str, Any], th: Thresholds,
         toggles: tuple[bool, bool],
         values: tuple[float | None, float | None]) -> None:
    """샘플 1건. c 는 `alerts._Step`(sb, device_uuid, meta, now_mono, now_wall).

    values: v4 와 같은 규칙으로 읽은 (온도, 습도) — `alerts._sample_values(row)`(R1). row 는 fan·relay 용.
    """
    vent_on, mist_on = toggles
    tr = st.tracker
    temp, humid = values
    tele_fan = row.get("fan")
    tele_fan = tele_fan if isinstance(tele_fan, str) else None

    def inp(fan: str | None, hold: bool) -> AutoInput:
        return AutoInput(
            mono=c.now_mono, wall=c.now_wall, temp=temp, humid=humid, fan=fan,
            has_fan=tele_fan is not None, has_relay=row.get("relay") is not None, th=th,
            vent_on=vent_on, mist_on=mist_on, schedule_hold=hold,
        )

    if not vent_on and not mist_on and tr.phase != "owned":
        # 비용 0 경로: DB 안 봄. 트래커는 토글 꺼짐 정리(R10)·타이머만
        st.sim_until = None
        tr.step(inp(tele_fan, False))
        return

    if not st.restored and not _restore(c, st):
        return
    hold = _schedule_hold(c, st)
    if hold is None:
        return
    if hold and not st.hold_prev and not dry_run():
        _supersede_pending(c)    # 구간 예약이 막 시작 — 아직 안 나간 자동 팬 명령이 예약 팬을 건드리지 않게
    st.hold_prev = hold

    fan = tele_fan
    if dry_run() and st.sim_until is not None:
        if tr.phase == "idle":
            st.sim_until = None
        else:
            fan = "ON" if c.now_mono < st.sim_until else "OFF"   # 실제 팬은 안 바뀌므로 모의 상태로 R6

    for cmd in tr.step(inp(fan, hold)):
        _issue(c, st, cmd)
    _sync_state(c, st)


def handle_ack(c: Any, st: AutoDeviceState, row: dict[str, Any]) -> None:
    """commands ACK 1건(행 = UPDATE 결과). c 는 sb, device_uuid, now_mono, now_wall 을 가진 것."""
    if not st.restored:
        return      # 이 브리지에서 자동 경로를 아직 안 돈 기기 — 복원이 DB 에서 읽는다
    action = row.get("action")
    source = row.get("source") or "manual"
    ok = row.get("result") == "ok"
    tr = st.tracker
    now = c.now_mono
    if source == AUTO_SOURCE:
        if action in ("fan_on", "fan_off", "mist"):
            tr.on_own_ack(action, ok, now)
    elif ok and action in FAN_ACTIONS:
        tr.on_other_ack(action, source, now)
        st.sim_until = None
        if not dry_run():
            _supersede_pending(c)
    if ok and action == "mist":
        tr.on_mist_ok(now)
    _sync_state(c, st)


__all__ = [
    "AUTO_SOURCE",
    "FAN_ACTIONS",
    "SCHEDULE_TTL_SEC",
    "AutoDeviceState",
    "auto_env_enabled",
    "device_allowed",
    "dry_run",
    "handle_ack",
    "span_active",
    "step",
]
