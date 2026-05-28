"""HTTP /health endpoint.

API 서버는 FastAPI 본체에 통합. MQTT 브리지는 별도로 띄움.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def register_health(app: FastAPI, label: str = "terra-api") -> None:
    """FastAPI 앱에 /health 라우트 등록."""

    @app.get(
        "/health",
        tags=["health"],
        summary="헬스 체크",
        responses={200: {"description": "정상"}},
    )
    async def health() -> JSONResponse:
        """nginx / Lightsail health check 용. 인증 불필요. 외부 의존성 검증은 안 함."""
        return JSONResponse({"ok": True, "service": label})
