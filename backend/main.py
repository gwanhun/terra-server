"""
terra-server FastAPI 진입점.

실행:
    uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000

systemd:
    ExecStart=/home/ubuntu/terra-server/.venv/bin/uvicorn backend.main:app \
        --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from fastapi.staticfiles import StaticFiles

from backend.health import register_health
from backend.routers import cameras, clips, devices, enclosures

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

_APP_DESCRIPTION = """
파충류/양서류 **사육장 통합 백엔드** REST API.

## 도메인
- **IoT 제어** — ESP32-S3 디바이스 페어링, 명령, 텔레메트리 메타
- **모션 영상** — ESP32-P4 / RPi 카메라 워커, R2 presigned URL 발급, motion clip 메타

영상 자체는 Cloudflare R2 에 워커가 직접 PUT/GET. 본 서버는 메타 + presigned URL 만 처리.

## 인증 모델 (3가지)

| 방식 | 헤더 | 발급 | 사용처 |
|------|------|------|--------|
| **JWT (사용자)** | `Authorization: Bearer <jwt>` | Supabase Auth (앱 로그인) | 사용자 엔드포인트 |
| **Device Token** | `Authorization: Bearer <device_token>` | `POST /devices/pair` (1회) | 디바이스 자체 호출 |
| **Camera Token** | `Authorization: Bearer <camera_token>` | `POST /cameras/pair` (1회) | 워커 영상 업로드 |

### Dev 모드 (`AUTH_MODE=dev`)
JWT 검증 스킵, `.env` 의 `DEV_USER_ID` 를 모든 요청에 적용. **프로덕션 금지.**

## Realtime / Supabase 직접 호출
명령 발행, 텔레메트리 구독, 알림 수신은 REST 가 아닌 Supabase Realtime / RLS 로 처리.
[docs/API.md](https://github.com/) §7 참고.

## OpenAPI 문서 접근
`/docs`, `/redoc`, `/openapi.json` 모두 HTTP Basic Auth 로 보호됨.
자격증명은 `.env` 의 `DOCS_BASIC_USER` / `DOCS_BASIC_PASS`.
"""

tags_metadata = [
    {"name": "health", "description": "헬스 체크."},
    {"name": "devices", "description": "ESP32-S3 디바이스 (센서/제어) 페어링 + CRUD."},
    {"name": "enclosures", "description": "사육장(상위 묶음) CRUD. device/camera 가 N:1 로 소속."},
    {"name": "cameras", "description": "카메라 워커 (ESP32-P4 / RPi) 페어링 + CRUD."},
    {
        "name": "clips",
        "description": (
            "모션 영상 클립. 워커는 Camera Token 으로 presigned PUT URL 받아 R2 직접 업로드, "
            "사용자는 JWT 로 presigned GET URL 받아 재생."
        ),
    },
]

app = FastAPI(
    title="terra-server",
    description=_APP_DESCRIPTION,
    version="0.1.0",
    openapi_tags=tags_metadata,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# OpenAPI 문서 Basic Auth 가드 — NestJS expressBasicAuth 와 동일 의도.
# /docs UI 만 막아도 /openapi.json 으로 스키마 전체 노출되므로 셋 다 같이 보호.
_docs_basic = HTTPBasic()


def _docs_guard(creds: HTTPBasicCredentials = Depends(_docs_basic)) -> None:
    user = os.getenv("DOCS_BASIC_USER", "")
    pw = os.getenv("DOCS_BASIC_PASS", "")
    # 타이밍 공격 방지: 항상 양쪽 비교 후 AND
    ok_user = secrets.compare_digest(creds.username, user)
    ok_pw = secrets.compare_digest(creds.password, pw)
    if not (user and pw and ok_user and ok_pw):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="docs 인증 실패",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/openapi.json", include_in_schema=False)
def _openapi(_: None = Depends(_docs_guard)) -> dict:
    return app.openapi()


@app.get("/docs", include_in_schema=False)
def _swagger_ui(_: None = Depends(_docs_guard)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="terra-server — Swagger UI")


@app.get("/redoc", include_in_schema=False)
def _redoc(_: None = Depends(_docs_guard)):
    return get_redoc_html(openapi_url="/openapi.json", title="terra-server — ReDoc")


# 정적 웹페이지(web/index.html) 부팅용 공개 설정.
# 노출되는 두 값(SUPABASE_URL, anon key)은 RLS 가 보호 → 클라이언트 공개 안전.
# SERVICE_ROLE_KEY 등 백엔드 전용 비밀은 절대 포함 금지.
@app.get("/web-config", include_in_schema=False)
def _web_config() -> dict:
    return {
        "supabaseUrl": os.getenv("SUPABASE_URL", ""),
        "supabasePublishableKey": os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
    }


# CORS — 앱(웹/모바일) 도메인 허용
_allowed = os.getenv("APP_ORIGINS", "").strip()
origins = [o.strip() for o in _allowed.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_health(app, label="terra-api")
app.include_router(devices.router)
app.include_router(enclosures.router)
app.include_router(cameras.router)
app.include_router(clips.camera_clips_router)
app.include_router(clips.enclosure_clips_router)
app.include_router(clips.clips_router)

# 정적 웹 콘솔 — 루트(/) 에 마운트.
# 라우터들 다음에 등록해야 /devices, /cameras, /web-config 등이 우선 매칭됨.
# html=True 면 / 요청 시 index.html 자동 서빙.
_WEB_DIR = REPO_ROOT / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
