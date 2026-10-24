"""
Supabase 싱글톤 클라이언트 (service_role).

petcam-lab/backend/supabase_client.py 패턴 그대로.

## 왜 service_role?
백엔드는 모든 디바이스의 telemetry/commands/alerts 를 INSERT/UPDATE 해야 하니
RLS 바이패스 가능한 service_role 키 필요. 절대 클라이언트(앱) 에 노출 금지.

앱(Flutter/웹) 은 anon 키 + JWT → RLS 가 본인 디바이스만 노출.

## 왜 lru_cache?
create_client 는 HTTP 세션 생성 비용. 매 요청마다 새로 만들면 커넥션 폭증.
프로세스 스코프 싱글톤으로 재사용.

## HTTP 계층을 왜 손대나 (2026-10-24)
supabase-py 가 만드는 httpx 세션은 **http2=True** 라 이 프로세스의 모든 스레드
(브리지 6개: paho 수신 / 디스패처 / 예약 / 오프라인 / OTA / 푸시)가 TCP 연결 **하나**에
멀티플렉싱된다. Supabase 게이트웨이가 그 연결을 끊으면(GOAWAY) 그 순간 날아가던 요청이
`httpx.RemoteProtocolError: Server disconnected` 로 죽는다. 운영 journal 에서 6일간
3만 줄 = **분당 1.7회꼴**로 확인됐고(2026-10-19~24), 그 결과:

- 예약 명령 유실 3건 (10/20 분무, 10/22 fan_on, 10/24 fan_off → 환기팬 7시간 ON)
- telemetry/ack/alert 44건이 "미페어링" 으로 오판돼 버려짐
- telemetry INSERT·last_seen UPDATE 실패 다수 (센서 데이터 구멍)

postgrest 내장 재시도는 **GET + HTTP 503/520 응답만** 보기 때문에 이 예외는 안 걸린다.
httpx 의 `HTTPTransport(retries=)` 도 **연결 수립 단계만** 재시도해서 해당 없다.
그래서 세션을 직접 두 가지로 바꾼다:

1. `http2=False` — 요청이 작고 드물어 HTTP/2 이득이 없는데 단일 실패점만 만든다.
   HTTP/1.1 이면 끊긴 연결 하나가 요청 하나만 버리고, 풀이 다음 요청에 새 연결을 준다.
2. GET/HEAD 재시도 — "Server disconnected" 는 응답을 한 바이트도 못 받은 상태라 읽기
   재시도가 안전하다. 쓰기는 서버가 이미 처리했는지 알 수 없어 전송 계층에서 재시도하지
   않고, 멱등이 보장된 자리만 `retry_idempotent()` 로 감싼다.

덧붙여 기본 타임아웃이 120초라 DB 가 한 번 멈추면 1초 폴링 루프가 2분간 정지한다 — 줄인다.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

import httpx
from dotenv import load_dotenv
from supabase import Client, create_client

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

_PLACEHOLDER_PATTERNS = ("PASTE_", "your-service-role-key", "your-project")


class SupabaseNotConfigured(RuntimeError):
    """SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 누락 또는 placeholder."""


class SupabaseClientLayoutChanged(RuntimeError):
    """supabase-py/httpx 내부 구조가 바뀌어 HTTP 하드닝을 적용할 수 없음.

    조용히 넘기면 http2 단일 연결로 되돌아가 2026-10-24 유실이 재발하므로 기동을 막는다.
    """


# ---------- HTTP 계층 (위 "HTTP 계층을 왜 손대나" 참고) ----------

# postgrest 기본 120초 → 명시 타임아웃. read 15초는 콘솔 집계 쿼리 중 가장 느린 것보다
# 넉넉하고, 디스패처(1초 폴링·명령 TTL 10초)가 멈춰 있는 시간도 그만큼으로 묶인다.
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

# 브리지 스레드 6개 + API 워커. keepalive_expiry 는 httpx 기본(5초) 그대로 — 길게 잡으면
# 서버가 이미 닫은 연결을 재사용할 확률이 올라간다.
_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=5.0)

# 재시도 대상: 응답을 한 바이트도 못 받고 **즉시** 끊긴 전송 실패들. 서버가 요청을
# 처리했는지는 알 수 없으므로 "멱등한 요청만" 이라는 조건이 붙는다.
#
# ReadTimeout/PoolTimeout 은 일부러 제외한다 — 느리게 실패하는 쪽이라 재시도하면 한 요청이
# read 타임아웃 × 시도 횟수만큼 스레드를 잡는다. 디스패처는 1초 폴링이라 그냥 실패를 받고
# 다음 폴링에서 다시 하는 게 낫다 (명령은 PR #24 의 pending 유지 경로가 이미 덮는다).
_RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.RemoteProtocolError,   # ← 2026-10-24 원인 (HTTP/2 GOAWAY / Server disconnected)
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

# 전송 계층에서 자동 재시도할 메서드 — 본문이 없고 서버 상태를 바꾸지 않는 것만.
_RETRY_METHODS = frozenset({"GET", "HEAD"})

RETRY_ATTEMPTS = 3            # 최초 1회 + 재시도 2회
RETRY_BASE_DELAY_SEC = 0.2    # 0.2s → 0.4s, 각 ±50% jitter

T = TypeVar("T")


def _retry_delay(attempt: int) -> float:
    """attempt(1부터) 번째 실패 뒤 대기 초. 지수 + jitter.

    jitter 를 넣는 이유: 연결 하나가 끊기면 여러 스레드가 동시에 실패하는데, 고정 간격이면
    재시도도 같이 몰려 새 연결에 또 부하를 준다.
    """
    return RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)) * (0.5 + random.random())


class _RetryingTransport(httpx.BaseTransport):
    """읽기(GET/HEAD)만 전송 실패에서 재시도하는 transport 래퍼.

    쓰기(POST/PATCH/DELETE)는 재시도하지 않는다 — 서버가 요청을 처리한 뒤 응답만 못 돌아온
    경우를 구분할 수 없어 명령 중복 발행·중복 INSERT 가 될 수 있다. 멱등이 보장된 쓰기는
    호출부에서 `retry_idempotent()` 로 감싼다.
    """

    def __init__(self, inner: httpx.BaseTransport, attempts: int = RETRY_ATTEMPTS) -> None:
        self._inner = inner
        self._attempts = attempts

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._inner.handle_request(request)
            except _RETRYABLE_EXC as exc:
                if request.method not in _RETRY_METHODS or attempt >= self._attempts:
                    raise
                delay = _retry_delay(attempt)
                logger.warning(
                    "DB %s %s 전송 실패(%d/%d, %s) — %.2fs 뒤 재시도",
                    request.method, request.url.path, attempt, self._attempts,
                    type(exc).__name__, delay,
                )
                time.sleep(delay)

    def close(self) -> None:
        self._inner.close()


def retry_idempotent(fn: Callable[[], T], *, label: str, attempts: int = RETRY_ATTEMPTS) -> T:
    """멱등한 DB 쓰기를 전송 실패에서 재시도. **멱등이 확실한 호출만** 감쌀 것.

    읽기는 `_RetryingTransport` 가 이미 처리하므로 감쌀 필요 없다.
    - 안전: 같은 값으로 덮어쓰는 UPDATE (ack 의 status/result/acked_at 처럼).
    - 금지: INSERT (중복 행), 읽은 값에 기반해 더하는 UPDATE.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except _RETRYABLE_EXC as exc:
            if attempt >= attempts:
                raise
            delay = _retry_delay(attempt)
            logger.warning(
                "DB %s 전송 실패(%d/%d, %s) — %.2fs 뒤 재시도",
                label, attempt, attempts, type(exc).__name__, delay,
            )
            time.sleep(delay)


def _harden_postgrest_session(client: Client) -> None:
    """postgrest 세션을 http2 없는 재시도 transport 로 교체 + 타임아웃 축소.

    supabase-py 2.30 은 httpx 클라이언트 주입을 `ClientOptions(httpx_client=...)` 로만
    받는데 그 클라이언트는 auth/storage/functions 와 **공유**된다 (base_url 이 서로 달라
    postgrest 용 base_url 을 넣으면 나머지가 깨진다). 그래서 postgrest 세션이 만들어진
    뒤에 그 세션만 교체한다.

    `_transport` 는 httpx 내부 속성이다 — pyproject 에서 httpx/supabase 버전을 핀하고,
    tests/test_supabase_http.py 가 교체가 실제로 먹었는지 검증한다. 구조가 바뀌면
    SupabaseClientLayoutChanged 로 기동을 막는다 (조용히 http2 로 되돌아가면 안 된다).
    """
    session = getattr(client.postgrest, "session", None)
    if not isinstance(session, httpx.Client):
        raise SupabaseClientLayoutChanged(
            "postgrest.session 이 httpx.Client 가 아님 — supabase-py 업그레이드 확인"
        )
    stale = getattr(session, "_transport", None)
    if not isinstance(stale, httpx.BaseTransport):
        raise SupabaseClientLayoutChanged(
            "httpx.Client._transport 없음 — httpx 업그레이드 확인"
        )

    session.timeout = _TIMEOUT
    # HTTPTransport(retries=) 는 연결 수립 단계만 재시도한다 (RemoteProtocolError 는 해당
    # 없음) — 응답 대기 중 끊기는 건 위 _RetryingTransport 가 맡는다.
    session._transport = _RetryingTransport(
        httpx.HTTPTransport(http2=False, retries=1, limits=_LIMITS)
    )
    stale.close()
    logger.info(
        "Supabase HTTP: http2 off, read timeout %.0fs, 읽기 재시도 %d회",
        _TIMEOUT.read, RETRY_ATTEMPTS,
    )


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    load_dotenv(REPO_ROOT / ".env")

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise SupabaseNotConfigured(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 환경변수 필요. .env 확인."
        )
    if any(p in url for p in _PLACEHOLDER_PATTERNS) or any(
        p in key for p in _PLACEHOLDER_PATTERNS
    ):
        raise SupabaseNotConfigured(
            "SUPABASE_URL / SERVICE_ROLE_KEY 가 placeholder 상태. "
            "Supabase 대시보드 > Settings > API 에서 실제 값 복사."
        )

    client = create_client(url, key)
    _harden_postgrest_session(client)
    return client


def reset_client_cache() -> None:
    get_supabase_client.cache_clear()
