#!/usr/bin/env python
"""
OTA 테스트 보조 — 릴리스·작업·보드 보고 상태를 한 화면에.

    uv run python scripts/ota_status.py             # 릴리스 + 최근 작업 + OTA 지원 보드
    uv run python scripts/ota_status.py --watch     # 5초마다 갱신 (prepare → ready → applying → verified 추적)
    uv run python scripts/ota_status.py --job <id>  # 작업 1건 상세

보드 보고(`capabilities.ota`, `fw`)는 heartbeat(카메라 15초 / 기기 3초)로 들어오므로 리플래시 직후엔 최대
수십 초 뒤에 보인다. 작업 상태 전이는 backend/ota_service.py 참고.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")


def _age(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    s = int((datetime.now(timezone.utc) - d).total_seconds())
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def show(sb, job_id: str | None) -> None:
    rels = sb.table("firmware_releases").select("id, target, version, size_bytes, created_at, notes") \
        .order("created_at", desc=True).limit(10).execute().data or []
    ver_by_id = {r["id"]: r["version"] for r in rels}

    print("── 릴리스 (최근 10)")
    for r in rels:
        print(f"  {r['target']:12} {r['version']:28} {r['size_bytes']:>9}B  {r['id']}  {r.get('notes') or ''}")

    print("── OTA 지원 보드 (capabilities.ota=true)")
    for t, col, sys_col in (("cameras", "camera_id", "clip_stats"), ("devices", "device_id", "sys_state")):
        rows = sb.table(t).select(f"id, {col}, firmware_ver, is_online, last_seen_at, capabilities, {sys_col}") \
            .is_("unlinked_at", "null").order("last_seen_at", desc=True).limit(40).execute().data or []
        for r in rows:
            caps = r.get("capabilities") or {}
            if not caps.get("ota"):
                continue
            s = r.get(sys_col) or {}
            if t == "cameras":
                s = s.get("sys") or {}
            print(f"  {t:8} {r[col]:16} fw={str(r.get('firmware_ver')):26} on={'●' if r.get('is_online') else '○'} "
                  f"seen={_age(r.get('last_seen_at')):>4} rssi={s.get('rssi')} int_largest={s.get('int_largest')} "
                  f"uptime={s.get('uptime_s')} reset={s.get('reset')}  uuid={r['id']}")

    q = sb.table("ota_jobs").select("*").order("created_at", desc=True)
    q = q.eq("id", job_id) if job_id else q.limit(15)
    jobs = q.execute().data or []
    print("── 작업" + (" 상세" if job_id else " (최근 15)"))
    for j in jobs:
        print(f"  {j['created_at'][5:19]} {j['kind']:6} {j['target_uuid'][:8]}… {ver_by_id.get(j['release_id'], j['release_id'][:8]):26} "
              f"{j['status']:11} {j.get('pct', 0):3}%  err={j.get('error') or '-'}  forced={j.get('forced')}  "
              f"upd={_age(j.get('updated_at'))} applied={_age(j.get('applied_at'))}  id={j['id']}")
        if job_id:
            for k in ("prepare_msg_id", "apply_msg_id", "prev_version", "issued_by", "finished_at"):
                print(f"      {k}: {j.get(k)}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--watch", action="store_true")
    p.add_argument("--job")
    a = p.parse_args()
    from postgrest.exceptions import APIError

    from backend.supabase_client import get_supabase_client
    sb = get_supabase_client()
    try:
        sb.table("ota_jobs").select("id").limit(1).execute()
    except APIError as exc:
        if "PGRST205" in str(exc):
            print("ota_jobs 테이블 없음 — migrations/2026-10-07_firmware_ota.sql 을 Supabase SQL Editor 에서 먼저 적용"
                  " (docs/OTA_TEST_PLAN_2026-10-07.md §0-4)", file=sys.stderr)
            return 2
        raise
    while True:
        if a.watch:
            os.system("clear")
            print(datetime.now().strftime("%H:%M:%S"))
        show(sb, a.job)
        if not a.watch:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
