#!/usr/bin/env python3
"""Supabase Auth 테스트 계정 일괄 생성 / 삭제.

언제 쓰나:
- 앱 QA·베타 테스터용 로그인 계정을 한 번에 여러 개 만들 때.
- 생성한 계정은 user_metadata.test_account=true 로 표시되어 --delete 로 한 번에 정리 가능.

사용 (프로젝트 루트, .env 의 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 사용):
    uv run python scripts/create_test_accounts.py                       # 30개, 공통 비밀번호 자동 생성
    uv run python scripts/create_test_accounts.py --count 5 --password 'Test1234!'
    uv run python scripts/create_test_accounts.py --dry-run             # 만들 목록만 출력
    uv run python scripts/create_test_accounts.py --delete              # test_account 메타 붙은 계정 전부 삭제

결과 CSV: storage/test_accounts_<날짜>.csv (storage/ 는 .gitignore — 커밋되지 않음)

왜 admin.create_user?
- signUp 은 이메일 확인 메일이 나가고 rate limit 에 걸린다. service_role 의 admin API 는
  email_confirm=True 로 바로 로그인 가능한 상태로 만들고, 메일도 안 보낸다.
"""

from __future__ import annotations

import argparse
import csv
import secrets
import string
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.supabase_client import get_supabase_client  # noqa: E402

META_FLAG = "test_account"


def _gen_password(length: int = 14) -> str:
    # 영문 대/소문자 + 숫자만 (앱에서 타이핑하기 쉽게, 특수문자 제외)
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _list_all_users(admin):
    users = []
    page = 1
    while True:
        batch = admin.list_users(page=page, per_page=1000)
        users.extend(batch)
        if len(batch) < 1000:
            return users
        page += 1


def cmd_create(args: argparse.Namespace) -> int:
    emails = [f"{args.prefix}{i:02d}@{args.domain}" for i in range(1, args.count + 1)]
    password = args.password or _gen_password()

    if args.dry_run:
        print(f"[dry-run] 생성 예정 {len(emails)}개, 비밀번호={password}")
        for e in emails:
            print(" ", e)
        return 0

    admin = get_supabase_client().auth.admin
    existing = {u.email: u for u in _list_all_users(admin) if u.email}

    out_path = REPO_ROOT / "storage" / f"test_accounts_{date.today().isoformat()}.csv"
    out_path.parent.mkdir(exist_ok=True)

    created = skipped = failed = 0
    rows: list[tuple[str, str, str, str]] = []
    for i, email in enumerate(emails, start=1):
        if email in existing:
            skipped += 1
            rows.append((email, "(기존 계정 — 비밀번호 유지)", existing[email].id, "skipped"))
            print(f"  skip   {email} (이미 존재)")
            continue
        try:
            res = admin.create_user(
                {
                    "email": email,
                    "password": password,
                    "email_confirm": True,
                    "user_metadata": {META_FLAG: True, "seq": i, "display_name": f"테스터{i:02d}"},
                }
            )
        except Exception as exc:  # supabase_auth 예외 종류가 여러 개라 메시지만 기록
            failed += 1
            rows.append((email, "", "", f"failed: {exc}"))
            print(f"  FAIL   {email}: {exc}")
            continue
        created += 1
        rows.append((email, password, res.user.id, "created"))
        print(f"  create {email} ({res.user.id})")

    if created:
        # 새로 만든 게 있을 때만 기록 — 재실행이 기존 CSV 의 비밀번호를 덮어쓰지 않게 신규 파일명 사용
        if out_path.exists():
            out_path = out_path.with_name(out_path.stem + f"_{secrets.token_hex(2)}.csv")
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["email", "password", "user_id", "status"])
            w.writerows(rows)
        print(f"\n생성 {created} / 기존 {skipped} / 실패 {failed}  →  {out_path.relative_to(REPO_ROOT)}")
        print(f"공통 비밀번호: {password}")
    else:
        print(f"\n생성 0 / 기존 {skipped} / 실패 {failed}  (CSV 미기록 — 기존 파일 유지)")
    return 1 if failed else 0


def cmd_delete(args: argparse.Namespace) -> int:
    admin = get_supabase_client().auth.admin
    targets = [
        u for u in _list_all_users(admin)
        if (u.user_metadata or {}).get(META_FLAG) is True
        and (u.email or "").endswith(f"@{args.domain}")
    ]
    if not targets:
        print("삭제할 테스트 계정 없음.")
        return 0
    print(f"삭제 대상 {len(targets)}개 (@{args.domain}, user_metadata.{META_FLAG}=true):")
    for u in targets:
        print(" ", u.email)
    if not args.yes:
        if input("정말 삭제? [y/N] ").strip().lower() != "y":
            print("취소.")
            return 2
    for u in targets:
        admin.delete_user(u.id)  # enclosures 등 owner_id FK 는 ON DELETE CASCADE
        print(f"  delete {u.email}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--prefix", default="terratest", help="이메일 로컬파트 접두어 (기본 terratest → terratest01@…)")
    p.add_argument("--domain", default="example.com")
    p.add_argument("--password", help="공통 비밀번호 (미지정 시 랜덤 14자 생성)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--delete", action="store_true", help="test_account 메타가 붙은 계정 전부 삭제")
    p.add_argument("--yes", action="store_true", help="--delete 시 확인 생략")
    args = p.parse_args()
    return cmd_delete(args) if args.delete else cmd_create(args)


if __name__ == "__main__":
    sys.exit(main())
