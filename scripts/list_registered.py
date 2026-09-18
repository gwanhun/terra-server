#!/usr/bin/env python3
"""테스트 계정별 등록된 기기·카메라 확인 (service_role, 읽기 전용).

phone_accounts CSV 의 계정(user_id) 기준으로 devices/cameras 를 owner_id 로 조회한다.

사용:
  uv run python scripts/list_registered.py                    # 최신 phone_accounts CSV 기준
  uv run python scripts/list_registered.py --csv storage/phone_accounts_2026-09-18.csv
  uv run python scripts/list_registered.py --all              # CSV 무관, 모든 소유자별 집계
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from backend.supabase_client import get_supabase_client  # noqa: E402


def _latest_csv() -> Path | None:
    files = sorted((REPO_ROOT / "storage").glob("phone_accounts_*.csv"))
    return files[-1] if files else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", type=Path)
    p.add_argument("--all", action="store_true", help="CSV 무관, 모든 owner 별 집계")
    args = p.parse_args()

    sb = get_supabase_client()
    devs = sb.table("devices").select("device_id,name,owner_id,is_online,enclosure_id,unlinked_at").execute().data
    cams = sb.table("cameras").select("camera_id,name,owner_id,is_online,enclosure_id,unlinked_at").execute().data
    d_by = defaultdict(list); c_by = defaultdict(list)
    for d in devs: d_by[d["owner_id"]].append(d)
    for c in cams: c_by[c["owner_id"]].append(c)

    if args.all:
        owners = sorted(set(d_by) | set(c_by))
        print(f"소유자 {len(owners)}명 · 기기 {len(devs)} · 카메라 {len(cams)}\n")
        for o in owners:
            print(f"[{o[:8]}]  기기 {len(d_by[o])} · 카메라 {len(c_by[o])}")
            for d in d_by[o]: print(f"    📟 {d['device_id']:16} {d['name']}" + (" (unlinked)" if d['unlinked_at'] else ""))
            for c in c_by[o]: print(f"    📷 {c['camera_id']:16} {c['name']}" + (" (unlinked)" if c['unlinked_at'] else ""))
        return 0

    csv_path = args.csv or _latest_csv()
    if not csv_path:
        sys.exit("phone_accounts CSV 없음. --all 로 전체 집계 가능.")
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    print(f"{csv_path.name}: 계정 {len(rows)}개\n")
    tot_d = tot_c = 0
    for r in rows:
        uid = r.get("user_id"); phone = r.get("phone") or r.get("email", "")
        ds = d_by.get(uid, []); cs = c_by.get(uid, [])
        tot_d += len(ds); tot_c += len(cs)
        mark = "" if (ds or cs) else "  (없음)"
        print(f"{phone:14} 기기 {len(ds)} · 카메라 {len(cs)}{mark}")
        for d in ds: print(f"    📟 {d['device_id']:16} {d['name']}  online={d['is_online']}")
        for c in cs: print(f"    📷 {c['camera_id']:16} {c['name']}  online={c['is_online']}")
    print(f"\n합계: 기기 {tot_d} · 카메라 {tot_c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
