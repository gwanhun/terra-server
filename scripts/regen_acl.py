#!/usr/bin/env python3
"""Mosquitto ACL 을 DB(devices + cameras) 기준으로 통째 재생성 + reload.

언제 쓰나:
- ACL 규칙이 바뀌었을 때 (예: 2026-09-08 카메라 telemetry 쓰기 권한 추가). 페어링/삭제 시엔
  API 가 자동으로 호출하므로, 규칙 변경 직후 기존 계정에 적용할 때만 수동 실행.

사용 (서버, 프로젝트 루트):
    .venv/bin/python scripts/regen_acl.py            # .env 자동 로드
    .venv/bin/python scripts/regen_acl.py --print    # 적용 없이 내용만 출력

MOSQUITTO_REGISTRY_ENABLED=true 여야 실제 적용된다 (헬퍼 sudo 권한은 DEPLOYMENT.md 참고).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from backend.mqtt import registry  # noqa: E402


def main() -> int:
    if "--print" in sys.argv:
        print(registry._build_acl_content())
        return 0
    if not registry._enabled():
        print("MOSQUITTO_REGISTRY_ENABLED 가 true 가 아님 — .env 확인. (--print 로 내용만 볼 수 있음)")
        return 2
    ok = registry.regenerate_acl()
    print("ACL 재생성 + reload:", "OK" if ok else "실패 (로그 확인)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
