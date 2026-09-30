"""
WebRTC offer 시간당 안전망 (2026-09-29, 설계 specs/camera-reliability-2026-09.md P4).

## 문제
현장 B 카메라에 앱이 stalled → 재연결을 ~1분 간격으로 4시간 반복(09-29 13:15~17:20, 248회) →
카메라가 매 ~16초 stalled, 영상 없음, 결국 SW:rtc_loop_stall 자가 재시작. 서버는 오퍼를 무제한 받았다.

## 왜 1시간 창인가 (7일 webrtc_connect_logs 실측, 28대)
| 창 | 현장 B | 나머지 최대 |
| 60초 | 6 | 8 |
| 10분 | 20 | 23 |
| 1시간 | 81 | 38 |
분·10분 창은 폭주와 정상 사용이 구분되지 않는다. 1시간 창에서만 갈린다 → 기본 60회/시.
근본 해결은 앱 재연결 백오프(docs/APP_WEBRTC.md) — 이건 서버 쪽 안전망.

- 카메라별 슬라이딩 1시간 창. 허용된 offer 만 기록 — 429 로 거절된 요청은 안 세서, 앱이 계속
  두드려도 차단이 무한 연장되지 않는다. Retry-After = 가장 오래된 기록이 창을 벗어날 때까지.
- WEBRTC_OFFER_LIMIT_PER_HOUR 로 조정(0 = 끔). 동시 시청 상한·최대 시청 시간은 owner 미결정이라 없음.
- 프로세스 메모리 — terra-api 는 단일 uvicorn 프로세스(ICE relay 버퍼와 같은 전제). 재시작 시 초기화.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_LIMIT_PER_HOUR = 60
WINDOW_SEC = 3600.0


class OfferRateLimiter:
    def __init__(self, limit: int, window_sec: float = WINDOW_SEC) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, camera_uuid: str, now: float | None = None) -> int | None:
        """허용이면 기록하고 None, 초과면 Retry-After 초(기록 안 함)."""
        if self.limit <= 0:
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(camera_uuid, deque())
            while hits and hits[0] <= now - self.window_sec:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1, math.ceil(hits[0] + self.window_sec - now))
            hits.append(now)
            return None


def _limit_from_env() -> int:
    raw = (os.getenv("WEBRTC_OFFER_LIMIT_PER_HOUR") or "").strip()
    if not raw:
        return DEFAULT_LIMIT_PER_HOUR
    try:
        return int(raw)
    except ValueError:
        logger.warning("WEBRTC_OFFER_LIMIT_PER_HOUR=%r 정수 아님 — 기본 %d", raw, DEFAULT_LIMIT_PER_HOUR)
        return DEFAULT_LIMIT_PER_HOUR


@lru_cache(maxsize=1)
def get_offer_limiter() -> OfferRateLimiter:
    """프로세스 싱글톤 (Depends 로 주입 — 테스트는 cache_clear)."""
    return OfferRateLimiter(_limit_from_env())
