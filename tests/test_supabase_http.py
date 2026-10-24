"""Supabase HTTP 계층 하드닝 (backend/supabase_client.py).

2026-10-19~24 운영 journal: httpx 세션이 `http2=True` 라 브리지 스레드 6개가 TCP 연결
**하나**를 공유했고, Supabase 게이트웨이가 그 연결을 끊을 때마다(6일간 3만 건) 그 순간
날아가던 요청이 `httpx.RemoteProtocolError: Server disconnected` 로 죽어 예약 명령 3건,
telemetry/ack/alert 44건이 유실됐다.

여기서 고정하는 계약:
- 읽기(GET/HEAD)는 전송 실패에서 재시도한다. 쓰기는 하지 않는다(중복 적용 위험).
- postgrest 세션은 http2 가 꺼져 있고 타임아웃이 120초가 아니다.
- supabase-py/httpx 내부 구조가 바뀌어 교체가 불가능하면 조용히 넘어가지 않고 예외다.
"""

from __future__ import annotations

import types

import httpx
import pytest
from supabase import create_client

from backend import supabase_client as sc


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """재시도 대기 제거 — 테스트는 횟수만 본다."""
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)


class _ScriptedTransport(httpx.BaseTransport):
    """호출마다 script 의 다음 항목을 돌려주거나(Response) 올린다(Exception)."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.method)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response)
        return item

    def close(self) -> None:
        self.closed = True


def _req(method: str = "GET") -> httpx.Request:
    return httpx.Request(method, "https://abc.supabase.co/rest/v1/devices")


def _disconnected() -> httpx.RemoteProtocolError:
    """운영에서 실제로 난 예외 — httpcore HTTP/2 스트림이 응답 전에 끊김."""
    return httpx.RemoteProtocolError("Server disconnected without sending a response.")


# ---------- 전송 계층 재시도 ----------


def test_get_retried_after_server_disconnected() -> None:
    inner = _ScriptedTransport([_disconnected(), httpx.Response(200, json=[])])
    resp = sc._RetryingTransport(inner).handle_request(_req())
    assert resp.status_code == 200
    assert inner.calls == ["GET", "GET"]


def test_get_gives_up_after_attempts() -> None:
    inner = _ScriptedTransport([_disconnected() for _ in range(sc.RETRY_ATTEMPTS)])
    with pytest.raises(httpx.RemoteProtocolError):
        sc._RetryingTransport(inner).handle_request(_req())
    assert len(inner.calls) == sc.RETRY_ATTEMPTS


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_writes_are_never_retried(method: str) -> None:
    """쓰기 재시도는 금지 — 서버가 이미 처리했는지 알 수 없어 명령 중복 발행이 된다."""
    inner = _ScriptedTransport([_disconnected(), httpx.Response(200, json=[])])
    with pytest.raises(httpx.RemoteProtocolError):
        sc._RetryingTransport(inner).handle_request(_req(method))
    assert inner.calls == [method]


def test_non_transport_error_not_retried() -> None:
    """프로토콜 위반처럼 재시도해도 같은 결과인 예외는 그냥 올린다."""
    inner = _ScriptedTransport([httpx.LocalProtocolError("bad request")])
    with pytest.raises(httpx.LocalProtocolError):
        sc._RetryingTransport(inner).handle_request(_req())
    assert inner.calls == ["GET"]


def test_close_is_forwarded() -> None:
    inner = _ScriptedTransport([])
    sc._RetryingTransport(inner).close()
    assert inner.closed


def test_retry_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    inner = _ScriptedTransport([_disconnected(), httpx.Response(200, json=[])])
    with caplog.at_level("WARNING", logger="backend.supabase_client"):
        sc._RetryingTransport(inner).handle_request(_req())
    assert "재시도" in caplog.text


# ---------- 멱등 쓰기 재시도 ----------


def test_retry_idempotent_returns_after_transport_failure() -> None:
    calls: list[int] = []

    def _fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _disconnected()
        return "done"

    assert sc.retry_idempotent(_fn, label="t") == "done"
    assert len(calls) == 3


def test_retry_idempotent_gives_up_after_attempts() -> None:
    calls: list[int] = []

    def _fn() -> None:
        calls.append(1)
        raise _disconnected()

    with pytest.raises(httpx.RemoteProtocolError):
        sc.retry_idempotent(_fn, label="t")
    assert len(calls) == sc.RETRY_ATTEMPTS


def test_retry_idempotent_does_not_retry_db_errors() -> None:
    """PK 충돌·권한 오류 같은 서버 응답은 재시도해도 같다 — 바로 올린다."""
    calls: list[int] = []

    def _fn() -> None:
        calls.append(1)
        raise RuntimeError("duplicate key value violates unique constraint")

    with pytest.raises(RuntimeError):
        sc.retry_idempotent(_fn, label="t")
    assert len(calls) == 1


# ---------- 세션 교체 ----------


def test_harden_disables_http2_and_shortens_timeout() -> None:
    """create_client 는 네트워크를 안 타므로 더미 URL/키로 실제 객체를 검사할 수 있다."""
    client = create_client("https://abc.supabase.co", "dummy-key")
    session = client.postgrest.session
    assert session._transport._pool._http2 is True          # supabase-py 기본값
    assert session.timeout.read == 120                      # postgrest 기본값

    sc._harden_postgrest_session(client)

    session = client.postgrest.session
    assert isinstance(session._transport, sc._RetryingTransport)
    assert session._transport._inner._pool._http2 is False
    assert session.timeout.read == sc._TIMEOUT.read < 120
    # base_url·인증 헤더는 그대로여야 한다 (transport 만 바꿨다).
    assert str(session.base_url).endswith("/rest/v1/")
    assert "apikey" in {k.lower() for k in session.headers}


def test_harden_raises_when_session_type_changes() -> None:
    fake = types.SimpleNamespace(postgrest=types.SimpleNamespace(session=object()))
    with pytest.raises(sc.SupabaseClientLayoutChanged):
        sc._harden_postgrest_session(fake)  # type: ignore[arg-type]


def test_harden_raises_when_transport_attr_missing() -> None:
    session = httpx.Client()
    del session._transport
    fake = types.SimpleNamespace(postgrest=types.SimpleNamespace(session=session))
    with pytest.raises(sc.SupabaseClientLayoutChanged):
        sc._harden_postgrest_session(fake)  # type: ignore[arg-type]


def test_read_timeout_is_not_retried() -> None:
    """느리게 실패하는 예외는 재시도 금지 — 한 요청이 read 타임아웃 × 시도 횟수를 잡는다."""
    inner = _ScriptedTransport([httpx.ReadTimeout("timed out")])
    with pytest.raises(httpx.ReadTimeout):
        sc._RetryingTransport(inner).handle_request(_req())
    assert inner.calls == ["GET"]
