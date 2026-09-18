#!/usr/bin/env python3
"""테스트 계정마다 개체(pets) 1마리 등록.

create_test_accounts.py 가 남긴 CSV(email,password,...) 를 읽어 각 계정으로 실제 로그인한 뒤
앱과 동일하게 RPC `redesign_save_pet_v1` 을 호출한다.

왜 service_role 로 pets 에 직접 INSERT 안 하나?
- pets 는 앱 팀 소유 테이블. 재설계 번들(2026-09-16) 이후 개체 쓰기는 소유자 락 + 이력 트리거 +
  이름 검증을 RPC 안에서 처리한다. 직접 INSERT 는 그 계약을 우회해서 앱이 기대하는 상태와 어긋날 수 있다.
- 사용자 세션(publishable key + 비밀번호 로그인) 으로 호출하면 auth.uid() 가 잡혀 RLS 도 그대로 검증된다.

사용 (프로젝트 루트):
    uv run python scripts/seed_test_pets.py                     # storage/test_accounts_*.csv 최신 파일
    uv run python scripts/seed_test_pets.py --csv storage/test_accounts_2026-09-17.csv
    uv run python scripts/seed_test_pets.py --dry-run

이미 활성 개체가 있는 계정은 건너뜀 (재실행 안전).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# species 테이블(운영) 에 존재하는 id/korean_name. 테스터별로 순환.
SPECIES = [
    ("crested-gecko", "크레스티드 게코", "노말"),
    ("leopard-gecko", "레오파드 게코", "노말"),
    ("fat-tailed-gecko", "펫테일 게코", "노말"),
    ("bearded", "비어디 드래곤", None),
]
SEXES = ["male", "female", "unknown"]


def _latest_csv() -> Path:
    files = sorted((REPO_ROOT / "storage").glob("test_accounts_*.csv"))
    if not files:
        sys.exit("storage/test_accounts_*.csv 없음 — create_test_accounts.py 먼저 실행")
    return files[-1]


def _sign_in_with_backoff(sb, email: str, password: str, retries: int = 5) -> None:
    """Supabase Auth 는 IP 당 로그인 시도를 5분에 30회로 제한한다 (기본값).
    30계정 연속 로그인이 딱 한계라 재실행 시 걸리기 쉬움 → 걸리면 60초 쉬고 재시도."""
    for attempt in range(retries):
        try:
            sb.auth.sign_in_with_password({"email": email, "password": password})
            return
        except Exception as exc:
            if "rate limit" not in str(exc).lower() or attempt == retries - 1:
                raise
            print(f"  wait   {email}: rate limit — 60초 대기 ({attempt + 1}/{retries})")
            time.sleep(60)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    csv_path = args.csv or _latest_csv()
    rows = [r for r in csv.DictReader(csv_path.open(encoding="utf-8")) if r["password"] and not r["password"].startswith("(")]
    print(f"{csv_path.relative_to(REPO_ROOT)}: 계정 {len(rows)}개")

    url, anon = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_PUBLISHABLE_KEY")
    if not url or not anon:
        sys.exit("SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY 필요 (.env)")

    created = skipped = failed = 0
    for i, r in enumerate(rows, start=1):
        sp_id, sp_name, morph = SPECIES[(i - 1) % len(SPECIES)]
        pet = {
            "id": str(uuid.uuid4()),
            "name": f"테스트{i:02d}",  # 10자 이하, 한글/ASCII 만 (redesign_validate_name)
            "species_id": sp_id,
            "species_name": sp_name,
            "morph": morph,
            "sex": SEXES[(i - 1) % len(SEXES)],
            "birth_date": "2025-06-01",
            "adoption_date": "2026-01-15",
            "weight": 5 + (i % 10),
            "memo": "QA 테스트 계정용 개체",
        }
        if args.dry_run:
            print(f"  [dry] {r['email']} → {pet['name']} / {sp_name} / {pet['sex']}")
            continue

        sb = create_client(url, anon)  # 계정별 새 클라이언트 (세션 섞임 방지)
        try:
            _sign_in_with_backoff(sb, r["email"], r["password"])
            existing = sb.table("pets").select("id,name").execute().data  # RLS: 본인 + 활성만
            if existing:
                skipped += 1
                print(f"  skip   {r['email']} (개체 있음: {existing[0]['name']})")
                continue
            res = sb.rpc(
                "redesign_save_pet_v1",
                {"p_pet": pet, "p_group_id": None, "p_expected_group_id": None, "p_request_id": str(uuid.uuid4())},
            ).execute()
            created += 1
            print(f"  create {r['email']} → {pet['name']} ({sp_name}) {res.data}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL   {r['email']}: {exc}")
        finally:
            try:
                sb.auth.sign_out()
            except Exception:  # noqa: BLE001 — 로그아웃 실패는 무시
                pass

    print(f"\n개체 생성 {created} / 기존 {skipped} / 실패 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
