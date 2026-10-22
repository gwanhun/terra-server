"""GET /console/access — 운영 콘솔(beta.html·live.html) 게이트.

- CONSOLE_ADMIN_EMAILS 비어 있으면 로그인한 아무 계정이나 admin (restricted=false)
- 채워져 있으면 JWT email 이 목록에 있어야 admin. dev 모드엔 JWT 가 없어 email=None → admin=false
- 로그인 없이는 401 (AUTH_MODE=prod 에서) — dev 모드 테스트에선 헤더 무시라 생략
"""

from __future__ import annotations


def test_access_open_when_no_allowlist(app_client, monkeypatch):
    monkeypatch.delenv("CONSOLE_ADMIN_EMAILS", raising=False)
    r = app_client.get("/console/access")
    assert r.status_code == 200
    body = r.json()
    assert body["admin"] is True and body["restricted"] is False


def test_access_denied_when_allowlist_and_no_email(app_client, monkeypatch):
    monkeypatch.setenv("CONSOLE_ADMIN_EMAILS", "owner@example.com, Ops@Example.com")
    r = app_client.get("/console/access")
    assert r.status_code == 200
    body = r.json()
    assert body["restricted"] is True and body["admin"] is False and body["email"] is None


def test_allowlist_parsing_is_case_insensitive_and_trimmed(monkeypatch):
    from backend.main import _console_admin_emails

    monkeypatch.setenv("CONSOLE_ADMIN_EMAILS", " A@x.com ,b@y.com,, ")
    assert _console_admin_emails() == {"a@x.com", "b@y.com"}
