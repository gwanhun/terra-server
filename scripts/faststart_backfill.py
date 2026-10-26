#!/usr/bin/env python3
"""
이미 R2 에 올라간 클립 mp4 를 faststart([ftyp][moov]...[mdat]) 로 일괄 변환한다.

    uv run python scripts/faststart_backfill.py --since 2026-10-20 --dry-run   # 몇 개나 옮겨야 하는지만
    uv run python scripts/faststart_backfill.py --since 2026-10-20             # 10/20 이후(키 날짜) 전부
    uv run python scripts/faststart_backfill.py --camera p4cam-2d854b2f --limit 50

- **범위를 꼭 정하고 돌릴 것.** 2026-10-26 기준 R2 에 mp4 5.5만 개·390GiB — 전부 변환하면 그만큼이
  서버를 거쳐(받고 올리고) Lightsail 전송량을 넘긴다. 키의 날짜(YYYY-MM-DD)로 거르므로 --since 는
  공짜(GET 없음)고, 최근 날짜부터 처리한다. 하루 ≈ 1,000~1,500개·12~15GiB.
- 키마다 앞 4KB Range GET 으로 판정 → 이미 앞이면 건너뜀(멱등, 여러 번 돌려도 됨).
- 변환은 같은 key 에 덮어쓰기. 크기·프레임 바이트 동일(backend/mp4_faststart.py), DB 는 안 건드림.
- 'terra-clips/clips/' 아래만(소프트 삭제된 deleted/ 와 앱 직접 녹화 recordings/ 는 대상 아님).
- 메타 등록 훅(backend/clip_faststart.py)이 놓친 파일(서버 재시작 등)도 이 스크립트가 잡는다.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from backend.clip_faststart import ensure_faststart  # noqa: E402
from backend.r2_client import list_objects  # noqa: E402

PREFIX = "terra-clips/clips/"
# terra-clips/clips/{camera_id}/{YYYY-MM-DD}/{HHMMSS}_{clip_id}.mp4
_KEY_DATE = re.compile(r"/(\d{4}-\d{2}-\d{2})/[^/]+\.mp4$")


def key_date(key: str) -> date | None:
    m = _KEY_DATE.search(key)
    try:
        return date.fromisoformat(m.group(1)) if m else None
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--camera", help="카메라 text id (예: p4cam-2d854b2f) 만")
    ap.add_argument("--limit", type=int, default=0, help="최대 처리(변환) 건수, 0=무제한")
    ap.add_argument("--since", type=date.fromisoformat, help="키 날짜 ≥ YYYY-MM-DD 만")
    ap.add_argument("--until", type=date.fromisoformat, help="키 날짜 ≤ YYYY-MM-DD 만")
    ap.add_argument("--dry-run", action="store_true", help="판정만, 덮어쓰기 없음")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    prefix = PREFIX + (f"{args.camera}/" if args.camera else "")
    counts: Counter[str] = Counter()
    t0 = time.monotonic()
    # 나열은 싸다(1,000개/요청). 날짜로 거른 뒤 최근 것부터 — 사람이 보는 건 최근 클립이다.
    keys: list[tuple[date, str]] = []
    for key, _modified in list_objects(prefix):
        if not key.endswith(".mp4"):
            continue
        d = key_date(key)
        if d is None:
            counts["no_date"] += 1
            continue
        if (args.since and d < args.since) or (args.until and d > args.until):
            continue
        keys.append((d, key))
    keys.sort(reverse=True)
    logging.info("대상 %d개 (날짜 없는 키 %d개 제외)", len(keys), counts["no_date"])
    for _d, key in keys:
        try:
            result = ensure_faststart(key, dry_run=args.dry_run)
        except (BotoCoreError, ClientError) as exc:
            logging.error("R2 오류 key=%s: %s", key, exc)
            result = "error"
        counts[result] += 1
        if result in ("converted", "would_convert"):
            logging.info("%s %s", result, key)
        if args.limit and counts["converted"] + counts["would_convert"] >= args.limit:
            logging.info("--limit %d 도달", args.limit)
            break
    logging.info("완료 %.0fs: %s", time.monotonic() - t0, dict(counts))
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
