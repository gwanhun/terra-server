"""
Supabase JWT 검증 + FastAPI Depends 체인.

petcam-lab/backend/auth.py 패턴을 그대로 차용. 도메인은 사육장 IoT.

## Dev / Prod 모드
- `AUTH_MODE=dev` (기본) — Authorization 헤더 무시, `DEV_USER_ID` 반환.
- `AUTH_MODE=prod` — `Authorization: Bearer <JWT>` 필수. 서명 검증 후 `sub` claim 반환.

## JWT 검증
Supabase Auth 비대칭 서명. 공개키는 JWKS 엔드포인트:
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`

알고리즘은 JWK 의 `alg` 필드 런타임 결정 (Supabase 는 ES256/P-256).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import jwt
from dotenv import load_dotenv
from fastapi import Header, HTTPException, status

REPO_ROOT = Path(__file__).resolve().parent.parent

_JWKS_TTL_SEC = 600  # 10분 TTL

_jwks_cache: dict[str, Any] = {"keys": None, "expires_at": 0.0}


class AuthError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _auth_mode() -> str:
    load_dotenv(REPO_ROOT / ".env")
    return os.getenv("AUTH_MODE", "dev").strip().lower()


def _dev_user_id() -> str:
    load_dotenv(REPO_ROOT / ".env")
    value = os.getenv("DEV_USER_ID", "").strip()
    if not value:
        raise AuthError("AUTH_MODE=dev 인데 DEV_USER_ID 가 비어있음. .env 확인.")
    return value


def get_jwks() -> list[dict[str, Any]]:
    now = time.monotonic()
    cached_keys = _jwks_cache["keys"]
    if cached_keys is not None and now < _jwks_cache["expires_at"]:
        return cached_keys

    load_dotenv(REPO_ROOT / ".env")
    jwks_url = os.getenv("SUPABASE_JWKS_URL", "").strip()
    if not jwks_url:
        raise AuthError("SUPABASE_JWKS_URL 이 비어있음 (AUTH_MODE=prod 에서 필수).")

    try:
        with urllib.request.urlopen(jwks_url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise AuthError(f"JWKS 조회 실패: {exc}") from exc

    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise AuthError("JWKS 응답에 keys 배열이 없음.")

    _jwks_cache["keys"] = keys
    _jwks_cache["expires_at"] = now + _JWKS_TTL_SEC
    return keys


def reset_jwks_cache() -> None:
    _jwks_cache["keys"] = None
    _jwks_cache["expires_at"] = 0.0


def verify_jwt(token: str) -> dict[str, Any]:
    load_dotenv(REPO_ROOT / ".env")
    issuer = os.getenv("SUPABASE_JWT_ISSUER", "").strip()

    try:
        headers = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError(f"JWT 헤더 파싱 실패: {exc}") from exc

    kid = headers.get("kid")
    if not kid:
        raise AuthError("JWT 헤더에 kid 가 없음.")

    keys = get_jwks()
    matching_key = next((k for k in keys if k.get("kid") == kid), None)
    if matching_key is None:
        # 캐시 무효화 후 1회 재시도 (키 로테이션 직후 시나리오)
        reset_jwks_cache()
        keys = get_jwks()
        matching_key = next((k for k in keys if k.get("kid") == kid), None)
    if matching_key is None:
        raise AuthError(f"JWKS 에서 kid={kid} 매칭되는 공개키를 못 찾음.")

    try:
        pyjwk = jwt.PyJWK(matching_key)
    except Exception as exc:
        raise AuthError(f"JWK → 공개키 변환 실패: {exc}") from exc

    algorithm = matching_key.get("alg") or pyjwk.algorithm_name
    if not algorithm:
        raise AuthError("JWK 에 alg 가 없고 algorithm_name 도 추론 불가.")

    try:
        payload = jwt.decode(
            token,
            key=pyjwk.key,
            algorithms=[algorithm],
            issuer=issuer if issuer else None,
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("JWT 만료됨.") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("JWT issuer 불일치.") from exc
    except jwt.InvalidSignatureError as exc:
        raise AuthError("JWT 서명 불일치.") from exc
    except jwt.PyJWTError as exc:
        raise AuthError(f"JWT 검증 실패: {exc}") from exc

    return payload


def get_jwt_payload(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    if not authorization:
        raise AuthError("Authorization 헤더가 없음.")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization 헤더 포맷은 'Bearer <token>' 이어야 함.")

    token = parts[1].strip()
    if not token:
        raise AuthError("Authorization 헤더에 토큰이 비어있음.")

    return verify_jwt(token)


def get_current_user_id(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI Depends: 현재 요청의 user_id (UUID str)."""
    mode = _auth_mode()
    if mode == "dev":
        return _dev_user_id()
    if mode == "prod":
        payload = get_jwt_payload(authorization=authorization)
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise AuthError("JWT payload 에 sub claim (user_id) 이 없음.")
        return sub
    raise AuthError(f"AUTH_MODE 값이 이상함: '{mode}'. 'dev' 또는 'prod' 만 허용.")
