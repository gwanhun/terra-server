"""
카메라 토큰 Bearer 인증.

워커(ESP32-P4/RPi)가 `/cameras/{id}/clips/*` 같은 엔드포인트 호출 시
`Authorization: Bearer <camera_token>` 으로 본인 검증.

페어링 시 발급된 평문 camera_token 의 bcrypt 해시가 `cameras.token_hash` 에 저장됨.
요청마다 URL path 의 camera UUID 로 row 조회 → bcrypt verify.

## JWT 와 분리한 이유
- 워커는 사용자 JWT 가 없음 (BLE 페어링 시 1회 받고 폐기)
- camera_token 은 디바이스 본인 식별용 (사용자 JWT 와 권한 범위 다름)
- 토큰 회전(token_rotate) 시 영향 범위 명확하게 분리
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Header, HTTPException, Path, status

from backend.crypto import verify_token
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


class CameraAuthError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise CameraAuthError("Authorization 헤더가 없음.")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise CameraAuthError("Authorization 헤더 포맷은 'Bearer <token>' 이어야 함.")

    return parts[1]


def get_authed_camera(
    camera_id: str = Path(..., description="cameras.id (UUID)"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    URL path 의 `{camera_id}` + Bearer 토큰 검증 후 camera row 반환.

    FastAPI Depends 로 사용:
        @router.post("/cameras/{camera_id}/clips/upload-url")
        def upload_url(camera: dict = Depends(get_authed_camera)): ...

    반환 row 에는 token_hash 포함 — 라우터는 sensitive 필드 응답 배제 책임.
    """
    token = _extract_bearer(authorization)

    sb = get_supabase_client()
    res = (
        sb.table("cameras")
        .select("id, owner_id, enclosure_id, camera_id, token_hash, name, model")
        .eq("id", camera_id)
        .single()
        .execute()
    )
    row = res.data
    if not row:
        # 의도적으로 401 (404 로 존재 여부 노출 안 함)
        raise CameraAuthError("카메라를 찾을 수 없거나 토큰 불일치.")

    token_hash = row.get("token_hash")
    if not token_hash or not verify_token(token, token_hash):
        raise CameraAuthError("카메라를 찾을 수 없거나 토큰 불일치.")

    return row
