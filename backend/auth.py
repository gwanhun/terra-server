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

## 관리자 + 대행(act-as) (2026-10-27)
사용자가 직접 가입·기기 등록을 하게 되면서 운영자가 테스터 비밀번호를 모른다. 그래서
- `.env ADMIN_USER_IDS` (쉼표 구분 auth.users.id) 에 든 계정만 관리자.
- 관리자가 `X-Act-As-User: <uuid>` 헤더를 붙이면 `get_current_user_id` 가 **그 사용자 id 를 돌려준다**
  → 기존 소유자 검사 라우터(/devices, /cameras, OTA, 해제…)가 코드 변경 없이 그 사용자 자격으로 동작.
- 관리자가 아닌데 헤더를 붙이면 403. 대행 호출은 INFO 로그로 남긴다(누가 누구 것을 만졌는지).
- 왜 service_role 로 따로 /admin 라우터를 전부 만들지 않나: 라우터 20여 개를 복제하게 된다.
  대행은 "그 사용자가 한 것과 똑같은 권한" 이라 과한 권한도 없다.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import jwt
from dotenv import load_dotenv
from fastapi import Header, HTTPException, status

from backend.db_helpers import parse_uuid

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

ACT_AS_HEADER = "X-Act-As-User"

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


def _caller_user_id(authorization: str | None) -> str:
    """헤더(또는 dev 모드)로 확인한 **실제 호출자** user_id. 대행 헤더는 보지 않는다."""
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


def admin_user_ids() -> frozenset[str]:
    """`.env ADMIN_USER_IDS` — 쉼표 구분 auth.users.id. 비어 있으면 관리자 없음(기능 꺼짐)."""
    load_dotenv(REPO_ROOT / ".env")
    raw = os.getenv("ADMIN_USER_IDS", "")
    return frozenset(v.strip().lower() for v in raw.split(",") if v.strip())


def is_admin(user_id: str) -> bool:
    return user_id.lower() in admin_user_ids()


def _not_admin(user_id: str) -> HTTPException:
    # 호출자 uid 를 그대로 알려 준다 — 운영자가 .env ADMIN_USER_IDS 에 넣을 값을 바로 알 수 있게.
    # 자기 uid 는 JWT 에 이미 있는 값이라 비노출 대상이 아니다.
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"not admin (user_id={user_id}) — 서버 .env ADMIN_USER_IDS 에 추가하면 허용",
    )


def get_current_user_id(
    authorization: str | None = Header(default=None),
    x_act_as_user: str | None = Header(default=None, alias=ACT_AS_HEADER),
) -> str:
    """FastAPI Depends: 현재 요청의 user_id (UUID str).

    관리자(ADMIN_USER_IDS)가 `X-Act-As-User` 를 붙이면 그 사용자 id 를 돌려준다(대행).
    헤더가 비어 있거나 공백이면 없는 것으로 본다. 관리자가 아니면 403, UUID 아니면 400.
    """
    caller = _caller_user_id(authorization)
    target = (x_act_as_user or "").strip()
    if not target:
        return caller
    if not is_admin(caller):
        raise _not_admin(caller)
    if parse_uuid(target) is None:
        raise HTTPException(status_code=400, detail=f"{ACT_AS_HEADER} 는 UUID 여야 함")
    target = target.lower()
    if target != caller.lower():
        logger.info("act-as admin=%s target=%s", caller, target)
    return target


def require_admin(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI Depends: 관리자만 통과. 관리자 본인의 user_id 반환(대행 헤더 무시)."""
    caller = _caller_user_id(authorization)
    if not is_admin(caller):
        raise _not_admin(caller)
    return caller
