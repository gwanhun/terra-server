"""
v4 일일 분석 생성 스레드 — Stage P Task 3 (specs/stage-p-daily-analysis-v4.md).

카드 문구는 순수 모듈 `backend/daily_analysis.py` 가 만들고, 이 모듈은 **언제·누구에 대해**
만들지, 입력을 어떻게 모을지, 어디에 저장할지만 맡는다. 브리지 프로세스 안 데몬 스레드
(`mqtt_bridge_main` 이 시작·정지)이고 service_role 로 조회하므로 모든 조회에 device_id 를 건다.

## 켜짐 조건

`ALERTS_V4_ENABLED`(Stage N) 와 `ENV_REPORTS_ENABLED`(기본 꺼짐) 둘 다 true/1/yes/on.
하나라도 꺼져 있으면 tick 은 테이블을 건드리지 않고 0 — 기획 확인 전 잠정 정책이 앱 정본으로
저장되지 않게.

`ALERTS_V4_SINCE`(v4 판정을 켠 시각, ISO8601)도 있어야 하고, 구간 시작이 그 이후인 날만 만든다.
v4 를 켜기 전 구간은 env_* 행이 없어 "이벤트 없는 날"로 잘못 분석되기 때문(켠 다음 날 아침
리포트가 08:00~켠 시각을 적정으로 표시). 값이 없거나 못 읽으면 생성하지 않는다.

## 2단계 생성 (P3)

tick 시각이 KST 08:00 이후면 report_date = 어제(KST) — 구간 어제 08:00 ~ 오늘 08:00.
08:00 전 tick 은 아무것도 안 한다(어제 리포트는 어제 냈고, 그보다 오래된 날은 소급 안 함).

| 상태 | 조건 | 동작 |
|------|------|------|
| 행 없음 | 그 기기 07:30 버킷이 집계됨 **또는** 08:15 지남 | 1차(`pass=1`) |
| 행 없음 | 09:05 지남(브리지가 08시대에 죽어 있었음) | 바로 2차 내용으로 1회(`pass=2`) |
| `pass=1` | 09:05 지남 | 재계산. 카드가 바뀌었을 때만 덮어쓰기(`pass=2`) — Stage N D11 소급 행 반영 |
| `pass=2` | - | 없음 |

2차 재계산 결과가 1차와 같으면 쓰지 않는다(앱 Realtime UPDATE 가 기기 수만큼 한 번 더 나가지
않게) — 그 행은 `pass=1` 로 남는다. 한 바퀴(전 기기)를 돌아 모든 기기가 2차까지 끝났으면 그
report_date 는 완료로 기억하고, 다음 날 08:00 까지 tick 은 조회 없이 0.

09:05 이후 처음 만드는 행을 1차·2차로 두 번 쓰지 않는 이유: 같은 시각·같은 입력이라 내용이
같고, 앱 Realtime 이벤트만 한 번 더 나간다. `pass=2` 는 "09:05 이후 입력으로 계산됨"이라는 뜻.

## 입력

- `alerts`: 기기, kind ∈ 온도·습도 v4 kind, 구간과 겹침(P4: 시작 < 구간 끝, 종료 없음 또는
  종료 > 구간 시작), `context.rule == "v4"`(파이썬 필터 — 기기·하루 단위라 행이 적다).
  겹치는 행의 `chain_root` 가 결과에 없으면(구간 전에 끝난 경고) id 로 추가 조회하고, 그 root
  시작 ~ 구간 시작 사이에 끝난 같은 chain 의 중간 행(무음 주의·D14 재경고)도 가져온다 → P11 한 줄
  (peak·ids 가 chain 전체를 반영).
- `telemetry_30m`: 구간 버킷(`bucket >= 시작`, `< 끝`).
- `humid_cycles`: `HUMID_CYCLES_ENABLED`(Stage O 소유) 가 참일 때만, 아니면 None(P9).

## 대상·커서 (P10)

`unlinked_at IS NULL` + `owner_id` 있음, id 순 최대 200기기/tick. 마지막 처리 id 를 커서로
기억해 다음 tick 은 그 뒤부터 — 항상 실패하는 기기가 뒤 기기를 굶기지 않는다. 끝 판정은
201개를 조회해서 한다(기기 수가 200의 배수여도 빈 tick 이 생기지 않게). 기기 단위 예외는 잡고
로그만 남긴다.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from backend.alerts import v4_enabled
from backend.daily_analysis import (
    HUMID_EVENT_KINDS,
    KST,
    TEMP_EVENT_KINDS,
    build_humid_card,
    build_temp_card,
    report_window,
)
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 300.0
BATCH_SIZE = 200                         # 한 tick 최대 기기 수 (P10)
FIRST_PASS_AT = time(8, 0)               # KST — 이 시각 전 tick 은 아무것도 안 함
FIRST_PASS_DEADLINE = time(8, 15)        # 07:30 버킷이 없어도 이때부터 1차
SECOND_PASS_AT = time(9, 5)              # 2차 재계산
LAST_BUCKET_OFFSET = timedelta(minutes=30)   # 구간 마지막 버킷 = 구간 끝 − 30분 (07:30)

EVENT_KINDS = TEMP_EVENT_KINDS + HUMID_EVENT_KINDS
_ALERT_COLUMNS = "id, device_id, kind, severity, triggered_at, resolved_at, context"
_BUCKET_COLUMNS = "bucket, t_a_avg, t_a_count, h_a_avg, h_a_count"

_UTC = timezone.utc
_TRUE = ("1", "true", "yes", "on")


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUE


def reports_enabled() -> bool:
    """`ALERTS_V4_ENABLED` 와 `ENV_REPORTS_ENABLED` 둘 다 참일 때만 리포트 생성."""
    return v4_enabled() and _flag("ENV_REPORTS_ENABLED")


def v4_since() -> datetime | None:
    """`ALERTS_V4_SINCE` — v4 판정을 켠 시각. 없거나 못 읽으면 None(리포트 생성 안 함)."""
    raw = (os.getenv("ALERTS_V4_SINCE") or "").strip()
    dt = _parse_iso(raw)
    if raw and dt is None:
        logger.warning("ALERTS_V4_SINCE 형식 오류(ISO8601 필요): %r", raw)
    return dt


def humid_cycles_enabled() -> bool:
    """Stage O 소유 플래그. 참이면 습도 카드에 humid_cycles 를 넘긴다(P9 해제)."""
    return _flag("HUMID_CYCLES_ENABLED")


def _iso(dt: datetime) -> str:
    return dt.astimezone(_UTC).isoformat()


def _parse_iso(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_UTC)


# ---------- 입력 조회 ----------

def _fetch_events(sb: Any, device_id: str, window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
    """구간과 겹치는 v4 이벤트 + 그 행들이 가리키는 (결과에 없는) chain_root 행."""
    start, end = (_iso(t) for t in window)
    def base() -> Any:   # P4 겹침의 OR 를 두 조회로 — 앞부분은 같다
        return (
            sb.table("alerts").select(_ALERT_COLUMNS)
            .eq("device_id", device_id).in_("kind", list(EVENT_KINDS)).lt("triggered_at", end)
        )

    ongoing = base().is_("resolved_at", "null").execute().data or []
    ended = base().gt("resolved_at", start).execute().data or []

    rows: dict[str, dict[str, Any]] = {}
    for r in [*ongoing, *ended]:
        ctx = r.get("context")
        if isinstance(ctx, dict) and ctx.get("rule") == "v4":
            rows[str(r["id"])] = r

    def is_v4(r: dict[str, Any]) -> bool:
        ctx = r.get("context")
        return r.get("kind") in EVENT_KINDS and isinstance(ctx, dict) and ctx.get("rule") == "v4"

    roots = {
        str(c["chain_root"]) for r in rows.values()
        if isinstance(c := r.get("context"), dict) and c.get("chain_root")
    }
    missing = roots - rows.keys()
    if missing:
        for r in (
            sb.table("alerts").select(_ALERT_COLUMNS)
            .eq("device_id", device_id).in_("id", sorted(missing)).execute().data or []
        ):
            if is_v4(r):
                rows[str(r["id"])] = r

    # chain 중간 행: root 시작 ~ 구간 시작 사이에 시작해 구간 전에 끝난 같은 chain 행
    root_starts = [
        ts for rid, r in rows.items() if rid in roots
        if (ts := _parse_iso(r.get("triggered_at"))) is not None
    ]
    earliest = min(root_starts, default=None)
    if earliest is not None and earliest < window[0]:
        for r in (
            sb.table("alerts").select(_ALERT_COLUMNS)
            .eq("device_id", device_id).in_("kind", list(EVENT_KINDS))
            .gte("triggered_at", _iso(earliest)).lt("triggered_at", start).execute().data or []
        ):
            ctx = r.get("context")
            if is_v4(r) and isinstance(ctx, dict) and str(ctx.get("chain_root") or "") in roots:
                rows.setdefault(str(r["id"]), r)
    return list(rows.values())


def _fetch_buckets(sb: Any, device_id: str, window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
    start, end = (_iso(t) for t in window)
    return (
        sb.table("telemetry_30m").select(_BUCKET_COLUMNS)
        .eq("device_id", device_id).gte("bucket", start).lt("bucket", end)
        .order("bucket").execute().data or []
    )


def _fetch_cycles(sb: Any, device_id: str, window: tuple[datetime, datetime]) -> list[dict[str, Any]] | None:
    if not humid_cycles_enabled():
        return None
    start, end = (_iso(t) for t in window)
    return (
        sb.table("humid_cycles").select("*")
        .eq("device_id", device_id).gte("started_at", start).lt("started_at", end)
        .execute().data or []
    )


def _has_last_bucket(sb: Any, device_id: str, window_end: datetime) -> bool:
    res = (
        sb.table("telemetry_30m").select("bucket")
        .eq("device_id", device_id).eq("bucket", _iso(window_end - LAST_BUCKET_OFFSET))
        .limit(1).execute()
    )
    return bool(res.data)


# ---------- 생성 ----------

def generate_daily(sb: Any, device: dict[str, Any], report_date: date, now: datetime, pass_no: int,
                   existing: dict[str, Any] | None = None) -> bool:
    """기기 하나의 report_date 카드를 계산해 upsert. 카드 둘 다 None 이어도 행은 저장.

    2차(`pass_no=2`)인데 `existing`(저장된 행) 카드와 같으면 쓰지 않고 False.
    """
    device_id = str(device["id"])
    window = report_window(report_date)
    events = _fetch_events(sb, device_id, window)
    buckets = _fetch_buckets(sb, device_id, window)
    cycles = _fetch_cycles(sb, device_id, window)
    temp = build_temp_card(events, buckets, window)
    humid = build_humid_card(events, buckets, window, cycles=cycles)
    temp_json = temp.to_json() if temp else None
    humid_json = humid.to_json() if humid else None
    if (pass_no == 2 and existing is not None
            and existing.get("temp_card") == temp_json and existing.get("humid_card") == humid_json):
        return False
    sb.table("env_daily_reports").upsert({
        "device_id": device_id,
        "report_date": report_date.isoformat(),
        "window_start": _iso(window[0]),
        "window_end": _iso(window[1]),
        "temp_card": temp_json,
        "humid_card": humid_json,
        "pass": pass_no,
        "generated_at": _iso(now),
    }, on_conflict="device_id,report_date").execute()
    return True


def _pass_to_run(sb: Any, device_id: str, existing_pass: int | None,
                 now_kst: datetime, window_end: datetime) -> int | None:
    """P3: 이번 tick 에 이 기기에 쓸 pass 번호(없으면 None)."""
    t = now_kst.time()
    if existing_pass is None:
        if t >= SECOND_PASS_AT:
            return 2
        if t >= FIRST_PASS_DEADLINE or _has_last_bucket(sb, device_id, window_end):
            return 1
        return None
    if existing_pass == 1 and t >= SECOND_PASS_AT:
        return 2
    return None


class AnalysisRunner:
    """별도 스레드에서 interval 주기 tick. start()/stop() 으로 라이프사이클.

    `sb` 는 테스트 주입용 — 없으면 첫 활성 tick 에 service_role 클라이언트를 가져온다.
    """

    def __init__(self, interval_sec: float = DEFAULT_INTERVAL_SEC, sb: Any = None) -> None:
        self._interval = interval_sec
        self._sb = sb
        self._cursor: str | None = None
        self._date: date | None = None        # 지금 돌고 있는 report_date
        self._settled: set[str] = set()       # 그 날짜에 2차까지 끝난 기기(쓰거나, 같아서 안 쓴)
        self._cycle_ok = True                 # 이번 바퀴에서 모든 기기가 settled 였는지
        self._done_date: date | None = None   # 전 기기 2차 완료 — 다음 날까지 조회 안 함
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _client(self) -> Any:
        if self._sb is None:
            self._sb = get_supabase_client()
        return self._sb

    def _targets(self, sb: Any) -> tuple[list[dict[str, Any]], bool]:
        """이번 배치와 "이 배치가 마지막인지". 한 개 더 조회해 끝을 판정한다."""
        q = (
            sb.table("devices").select("id, owner_id")
            .is_("unlinked_at", "null").not_.is_("owner_id", "null")
        )
        if self._cursor is not None:
            q = q.gt("id", self._cursor)
        rows = q.order("id").limit(BATCH_SIZE + 1).execute().data or []
        if len(rows) > BATCH_SIZE:
            rows = rows[:BATCH_SIZE]
            self._cursor = str(rows[-1]["id"])
            return rows, False
        self._cursor = None   # 끝까지 왔으면 다음 tick 은 처음부터
        return rows, True

    def tick(self, now: datetime) -> int:
        """1회 처리. 쓴 행 수를 돌려준다."""
        if not reports_enabled():
            return 0
        if now.tzinfo is None:
            now = now.replace(tzinfo=_UTC)
        now_kst = now.astimezone(KST)
        if now_kst.time() < FIRST_PASS_AT:
            return 0
        report_date = now_kst.date() - timedelta(days=1)
        window_start, window_end = report_window(report_date)
        since = v4_since()
        if since is None or window_start < since:
            return 0   # v4 를 켜기 전 구간 — "이벤트 없음"으로 분석하지 않는다
        if report_date == self._done_date:
            return 0
        if report_date != self._date:
            self._date, self._settled, self._cursor = report_date, set(), None
        if self._cursor is None:
            self._cycle_ok = True

        sb = self._client()
        devices, last_batch = self._targets(sb)
        written = 0
        todo = [d for d in devices if str(d["id"]) not in self._settled]
        existing: dict[str, dict[str, Any]] = {}
        if todo:
            existing = {
                str(r["device_id"]): r
                for r in (
                    sb.table("env_daily_reports").select("device_id, pass, temp_card, humid_card")
                    .eq("report_date", report_date.isoformat())
                    .in_("device_id", [str(d["id"]) for d in todo])
                    .execute().data or []
                )
            }
        for device in todo:
            device_id = str(device["id"])
            row = existing.get(device_id)
            try:
                pass_no = _pass_to_run(sb, device_id, row.get("pass") if row else None, now_kst, window_end)
                if pass_no is not None and generate_daily(sb, device, report_date, now, pass_no, row):
                    written += 1
                if pass_no == 2 or (row is not None and row.get("pass") == 2):
                    self._settled.add(device_id)
                else:
                    self._cycle_ok = False
            except Exception:  # noqa: BLE001 — 한 기기 실패가 나머지를 막지 않게
                self._cycle_ok = False
                logger.exception("일일 분석 생성 실패: device=%s date=%s", device_id, report_date)
        if last_batch and self._cycle_ok and now_kst.time() >= SECOND_PASS_AT:
            self._done_date = report_date
            logger.info("일일 분석 %s 전 기기 완료 — 다음 날 08:00 까지 쉼", report_date)
        return written

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="analysis-runner")
        self._thread.start()
        logger.info("analysis runner 시작 (interval=%.1fs, enabled=%s)", self._interval, reports_enabled())

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("analysis runner 정지")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.tick(datetime.now(_UTC))
                if n:
                    logger.info("일일 분석 %d건 저장", n)
            except Exception:  # noqa: BLE001
                logger.exception("analysis tick 실패")
            self._stop.wait(self._interval)


__all__ = [
    "AnalysisRunner",
    "BATCH_SIZE",
    "DEFAULT_INTERVAL_SEC",
    "generate_daily",
    "humid_cycles_enabled",
    "reports_enabled",
    "v4_since",
]
