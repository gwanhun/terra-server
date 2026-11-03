#!/usr/bin/env python
"""
v4 온습도 알림 롤백용 — 활성 v4 행(resolved_at NULL) 일괄 종료.

    uv run python scripts/close_v4_alerts.py                    # dry-run (기본) — 대상만 출력
    uv run python scripts/close_v4_alerts.py --apply            # 실제 종료
    uv run python scripts/close_v4_alerts.py --device <uuid>    # 기기 1대만

대상: kind ∈ {env_temp_high, env_temp_low, env_humid_low, env_humid_high} 이고
context.rule == "v4" 인 활성 행. 펌웨어·legacy·Stage O(env_vent_*) 행은 건드리지 않는다.
종료 = resolved_at 을 지금으로, context.peak_value 가 있으면 peak_diff 도 기록(분석이 "+None" 을 만들지 않게).

롤백 순서(specs/stage-n-env-alerts-v4.md "배포·검증·롤백" 5): `ALERTS_V4_ENABLED=false` + 브리지 재시작
**후에** 실행한다 — 브리지가 v4 로 돌고 있으면 다음 샘플에서 새 이벤트를 다시 연다.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.alerts import close_v4_row, forced_close_context, select_active_v4_rows  # noqa: E402


def run(sb: Any, *, apply: bool, device: str | None = None,
        now: datetime | None = None, out: Callable[[str], None] = print) -> int:
    """대상 행을 출력하고 apply 면 종료. 종료(또는 종료 예정) 행 수를 돌려준다."""
    now = now or datetime.now(timezone.utc)
    rows = select_active_v4_rows(sb, device)
    out(f"활성 v4 행 {len(rows)}개" + ("" if apply else " (dry-run — --apply 로 종료)"))
    done = 0
    for r in rows:
        ctx = forced_close_context(r)
        out(f"  {r.get('device_id')} {r['kind']:15} {ctx.get('level')!s:8} "
            f"triggered={r.get('triggered_at')} peak={ctx.get('peak_value')} "
            f"peak_diff={ctx.get('peak_diff')} id={r['id']}")
        if not apply:
            done += 1
            continue
        try:
            close_v4_row(sb, r, now.isoformat())
        except Exception as exc:  # noqa: BLE001 — supabase-py 예외 타입 넓음, 나머지 행은 계속
            out(f"    ! 종료 실패: {exc}")
            continue
        done += 1
    out(f"{'종료' if apply else '종료 예정'} {done}개")
    return done


def main() -> int:
    p = argparse.ArgumentParser(description="활성 v4 온습도 알림(env_*) 일괄 종료 — 기본 dry-run")
    p.add_argument("--apply", action="store_true", help="실제 종료 (기본은 dry-run)")
    p.add_argument("--device", help="devices.id(UUID) 하나만")
    a = p.parse_args()

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    from backend.supabase_client import get_supabase_client

    run(get_supabase_client(), apply=a.apply, device=a.device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
