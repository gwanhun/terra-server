#!/usr/bin/env python3
"""전화번호를 아이디(이메일 로컬파트)로 하는 테스트 계정 일괄 재생성.

- 이메일: <숫자만_전화번호>@example.com  (앱은 이메일/비밀번호 로그인 → 전화번호가 곧 아이디)
- 비밀번호: 123456 (Supabase 기본 최소 길이 6 충족)
- admin.create_user + email_confirm=True → 확인 메일 없이 바로 로그인 가능
- user_metadata.test_account=true 로 표시 → --delete 로 일괄 정리

사용:
    uv run python scripts/phone_test_accounts.py --recreate   # 기존 test_account 삭제 후 새로 생성
    uv run python scripts/phone_test_accounts.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from backend.supabase_client import get_supabase_client  # noqa: E402

META_FLAG = "test_account"
DOMAIN = "test.com"
PASSWORD = "123456"

RAW_PHONES = """
01044402908
01054428636
01036554738
01097361840
01047576410
010-9239-9175
01042718999
01088189632
01093052963
010 2672 8314
010-4659-0180
010-5391-1601
010-2489-6379
01023636275
01093115816
01048533907
01026448891
010 4357 6098
010 3768 9511
010-2112-0001
01057599851
01092637727
01072818228
01063097159
010-4095-1277
01028991691
010-9494-3606
01051912607
01072857420
01053995774
"""


def phones() -> list[str]:
    out = []
    for line in RAW_PHONES.strip().splitlines():
        digits = re.sub(r"\D", "", line)
        if digits:
            out.append(digits)
    return out


def _list_all(admin):
    users, page = [], 1
    while True:
        batch = admin.list_users(page=page, per_page=1000)
        users.extend(batch)
        if len(batch) < 1000:
            return users
        page += 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recreate", action="store_true", help="기존 test_account 삭제 후 생성")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    nums = phones()
    emails = [f"{n}@{DOMAIN}" for n in nums]
    dupes = {e for e in emails if emails.count(e) > 1}
    print(f"전화번호 {len(nums)}개 (중복: {sorted(dupes) or '없음'})")

    if args.dry_run:
        for n in nums:
            print(f"  {n}@{DOMAIN}  /  {PASSWORD}")
        return 0

    admin = get_supabase_client().auth.admin

    if args.recreate:
        # 이 프로젝트는 auth.users 하드 삭제가 전역으로 막혀 있음(500 Database error) → 소프트 삭제로
        # 로그인 불가·숨김 처리. 도메인 무관하게 test_account 표시된 계정 전부 대상.
        old = [u for u in _list_all(admin)
               if (u.user_metadata or {}).get(META_FLAG) is True and getattr(u, "deleted_at", None) is None]
        print(f"기존 test_account 소프트 삭제: {len(old)}개")
        for u in old:
            try:
                admin.delete_user(u.id, should_soft_delete=True)
                print(f"  soft-delete {u.email}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL soft-delete {u.email}: {exc}")

    existing = {u.email for u in _list_all(admin) if u.email}
    out_path = REPO_ROOT / "storage" / f"phone_accounts_{date.today().isoformat()}.csv"
    out_path.parent.mkdir(exist_ok=True)
    created = skipped = failed = 0
    rows = []
    for i, (num, email) in enumerate(zip(nums, emails), start=1):
        if email in existing:
            skipped += 1
            rows.append((num, email, PASSWORD, "", "skipped"))
            print(f"  skip   {email} (이미 존재)")
            continue
        try:
            res = admin.create_user({
                "email": email,
                "password": PASSWORD,
                "email_confirm": True,
                "user_metadata": {META_FLAG: True, "seq": i, "phone": num, "display_name": num},
            })
        except Exception as exc:  # noqa: BLE001
            failed += 1
            rows.append((num, email, PASSWORD, "", f"failed: {exc}"))
            print(f"  FAIL   {email}: {exc}")
            continue
        created += 1
        rows.append((num, email, PASSWORD, res.user.id, "created"))
        print(f"  create {email} ({res.user.id})")

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "email", "password", "user_id", "status"])
        w.writerows(rows)
    print(f"\n생성 {created} / 기존 {skipped} / 실패 {failed}  →  {out_path.relative_to(REPO_ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
