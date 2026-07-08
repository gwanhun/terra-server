"""R2 버킷의 실제 lifecycle rule(자동 만료삭제 정책)을 조회한다.

로컬에선 .env R2 키가 placeholder 라 실패할 수 있음 → 프로덕션 서버에서 실행:
    uv run python scripts/check_r2_lifecycle.py

lifecycle rule 이 있으면 며칠/어떤 prefix 대상인지 출력. 없으면 "설정 안 됨" 경고.
요청 2(즐겨찾기 클립 보존) 설계 전에 현재 만료정책을 확정하려는 용도.
"""

from botocore.exceptions import ClientError

from backend.r2_client import get_r2_bucket, get_r2_client


def main() -> None:
    client = get_r2_client()
    bucket = get_r2_bucket()
    print(f"버킷: {bucket}")

    try:
        resp = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if "NoSuchLifecycleConfiguration" in code:
            print(">>> Lifecycle rule 없음 — 자동 삭제 설정 안 돼 있음.")
            print("    (클립이 R2 에 계속 쌓이는 중. cleanup 도입 시 즐겨찾기 제외를 처음부터 반영.)")
            return
        raise

    rules = resp.get("Rules", [])
    print(f"Lifecycle rule {len(rules)}개:")
    for r in rules:
        exp = r.get("Expiration", {})
        days = exp.get("Days", "?")
        filt = r.get("Filter", {})
        print(f"  - id={r.get('ID')} status={r.get('Status')} "
              f"filter={filt} expire_after={days}일")
    print()
    print("※ filter 가 prefix 단위면 '즐겨찾기 개별 클립 제외'는 불가 → 요청 2 보존은 서버 배치로 전환 필요.")


if __name__ == "__main__":
    main()
