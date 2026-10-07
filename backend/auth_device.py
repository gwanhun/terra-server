"""
IoT 기기 토큰 Bearer 인증 (auth_camera 와 같은 모양).

기기(terra-iot-nano)가 서버 HTTP 엔드포인트를 직접 호출할 때 — 지금은 OTA 바이너리 다운로드
(`GET /firmware/jobs/{job_id}/bin`) 뿐 — `Authorization: Bearer <device_token>` 으로 본인 검증.

기기 토큰은 페어링 때 1회 발급된 MQTT 비밀번호(devices.token_hash 의 bcrypt 원문)와 같은 값이다.
HTTPS Bearer 로 재사용하는 건 Stage J 의 의도된 결정(specs/stage-j-ota.md 리스크 B5):
기기에 자격증명이 하나만 있어 NVS·페어링 응답 계약을 바꾸지 않아도 된다.

여기서는 "토큰 → 기기 행" 검증 함수만 두고 FastAPI Depends 는 라우터가 조립한다
(OTA 다운로드는 URL 에 job_id 가 오고 기기 UUID 는 job 에서 나오므로 path 의존성이 다르다).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status

from backend.crypto import verify_token
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


class DeviceAuthError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def extract_bearer(authorization: str | None) -> str:
    """`Authorization: Bearer <token>` 에서 토큰만. 형식이 다르면 401."""
    if not authorization:
        raise DeviceAuthError("Authorization 헤더가 없음.")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise DeviceAuthError("Authorization 헤더 포맷은 'Bearer <token>' 이어야 함.")
    return parts[1]


def verify_entity_token(kind: str, entity_uuid: str, token: str) -> dict[str, Any]:
    """kind('camera'|'device') 의 entity_uuid 행을 찾아 token 을 bcrypt 검증. 통과 시 행 반환.

    미존재·해제·불일치는 전부 401 (존재 여부 비노출, auth_camera 와 동일 규칙).
    """
    table = "cameras" if kind == "camera" else "devices"
    sb = get_supabase_client()
    res = (
        sb.table(table)
        .select("id, owner_id, token_hash, unlinked_at")
        .eq("id", entity_uuid)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or row.get("unlinked_at"):
        raise DeviceAuthError("기기를 찾을 수 없거나 토큰 불일치.")
    token_hash = row.get("token_hash")
    if not token_hash or not verify_token(token, token_hash):
        raise DeviceAuthError("기기를 찾을 수 없거나 토큰 불일치.")
    return row


__all__ = ["DeviceAuthError", "extract_bearer", "verify_entity_token"]
