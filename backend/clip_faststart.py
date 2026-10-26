"""
R2 에 올라간 클립 mp4 를 faststart 로 고쳐 같은 key 에 덮어쓴다(backend/mp4_faststart.py).

## 언제 도나
1. 메타 등록(POST /cameras/{id}/clips) 응답 **뒤** BackgroundTasks 로 — 카메라 HTTP 타임아웃이
   15초라 응답 안에서 15MB 를 받았다 올리면 카메라가 실패로 보고 처음부터 재업로드한다.
2. scripts/faststart_backfill.py — 이미 올라간 파일(0.4.14 이하 전 카메라) 일괄.

## 멱등·경합
먼저 앞 몇 KB 만 Range GET 해서 이미 moov 가 앞이면 아무것도 안 한다(0.4.15 카메라 파일은 여기서
끝). 메타 POST 재시도로 같은 key 가 두 번 들어와도 두 번째는 no-op 이거나 같은 바이트를 또 올릴
뿐이다. S3 PUT 은 객체 단위 원자적이라 앱이 그 사이 읽어도 옛 파일 아니면 새 파일이다(섞이지 않음).
파일 크기는 변하지 않으므로 motion_clips.file_size 는 손대지 않는다.

프로세스당 한 번에 하나만 변환한다(세마포어) — 29대가 동시에 올려도 메모리 20MB×N 이 되지 않게.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError

from backend.mp4_faststart import NotMp4, faststart, needs_faststart
from backend.r2_client import get_object_bytes, put_object_bytes

logger = logging.getLogger(__name__)

HEAD_BYTES = 4096          # 레거시는 ftyp(32)+mdat 헤더(8)로 판정 끝. 넉넉히.
_one_at_a_time = threading.Semaphore(1)

GetFn = Callable[..., bytes]
PutFn = Callable[[str, bytes, str], None]


def ensure_faststart(key: str, *, get_fn: GetFn = get_object_bytes,
                     put_fn: PutFn = put_object_bytes, dry_run: bool = False) -> str:
    """'already' | 'converted' | 'would_convert'(dry_run) | 'skipped'(mp4 아님). R2 오류는 raise."""
    head = get_fn(key, byte_range=(0, HEAD_BYTES - 1))
    need = needs_faststart(head)
    if need is False:
        return "already"
    if dry_run and need is True:
        return "would_convert"
    data = get_fn(key)
    try:
        out = faststart(data)
    except NotMp4 as exc:
        logger.warning("faststart 건너뜀 key=%s: %s", key, exc)
        return "skipped"
    if out is None:
        return "already"
    if dry_run:
        return "would_convert"
    put_fn(key, out, "video/mp4")
    logger.info("faststart 변환 key=%s bytes=%d", key, len(out))
    return "converted"


def ensure_faststart_safe(key: str) -> None:
    """BackgroundTasks 용 — 예외를 삼킨다(응답은 이미 나갔고, 못 고친 파일은 백필이 잡는다)."""
    with _one_at_a_time:
        try:
            ensure_faststart(key)
        except (BotoCoreError, ClientError):
            logger.exception("faststart R2 오류 key=%s (백필 스크립트가 재시도)", key)
        except Exception:  # noqa: BLE001 — 백그라운드 스레드에서 죽지 않게
            logger.exception("faststart 예기치 않은 오류 key=%s", key)
