"""
terra-server FastAPI 진입점.

실행:
    uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000

systemd:
    ExecStart=/home/ubuntu/terra-server/.venv/bin/uvicorn backend.main:app \
        --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from backend.auth import get_current_user_id, get_jwt_payload
from backend.health import register_health
from backend.live_session import LiveSessionReaper
from backend.r2_client import delete_object, list_objects
from backend.recording_cleanup import PendingRecordingReaper
from backend.routers import (
    cameras,
    clips,
    commands,
    devices,
    enclosures,
    firmware,
    lcd,
    ops,
    recordings,
    schedules,
    settings,
    webrtc,
)
from backend.routers.webrtc import close_camera_session
from backend.supabase_client import get_supabase_client
from backend.webrtc_relay import get_relay

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# supabase-py 가 모든 REST 호출을 httpx INFO 로 찍어 실제 경고가 묻힌다.
# 우리 코드 로그는 INFO 유지, HTTP 클라이언트만 WARNING (브리지도 동일 처리).
for _noisy in ("httpx", "httpcore", "hpack", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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
    {"name": "webrtc", "description": "카메라 라이브 스트리밍 WebRTC 시그널링."},
    {"name": "commands", "description": "액추에이터 명령 (물분무 등). 서버 검증 후 commands 큐잉."},
    {"name": "lcd", "description": "LCD 커스텀 텍스트. 서버가 비트맵 렌더 후 디바이스로 전송."},
    {"name": "schedules", "description": "예약 타이머 (daily/weekly). runner 가 due 시 명령 발행."},
    {
        "name": "clips",
        "description": (
            "모션 영상 클립. 워커는 Camera Token 으로 presigned PUT URL 받아 R2 직접 업로드, "
            "사용자는 JWT 로 presigned GET URL 받아 재생."
        ),
    },
    {
        "name": "recordings",
        "description": (
            "라이브 직접 녹화(앱이 폰에서 만든 ≤120초 mp4 + jpg). 등록 → presigned PUT 업로드 → "
            "complete → 재생 URL·삭제. 목록은 Supabase 직결 SELECT."
        ),
    },
    {
        "name": "ops",
        "description": (
            "운영 전용(서버 시크릿 `X-Ops-Secret`). petcam-lab 감시 스크립트가 베타 카메라를 "
            "재부팅. 사용자 JWT 로는 호출 불가."
        ),
    },
]

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """FastAPI lifespan — startup 에서 IceRelay (WebRTC ICE 릴레이) 시작.

    펌웨어 → 웹 방향 candidate 를 long-poll 로 전달하기 위해 1개의 long-lived
    MQTT subscriber 가 `esp32/+/ack` 를 구독한다. 자세한 동기는 backend/webrtc_relay.py.
    """
    # 운영 재부팅(2026-10-20): 시크릿 미설정이면 /ops 는 전부 401. 값은 로그 금지.
    if not os.getenv("OPS_REBOOT_SECRET", "").strip():
        logging.getLogger(__name__).warning("OPS_REBOOT_SECRET 미설정 — /ops 운영 재부팅 비활성(401)")
    relay = get_relay()
    loop = asyncio.get_running_loop()
    relay.start(loop)
    # 라이브 15분 상한 집행(2026-09-29): 만료 세션을 카메라에서 닫고 5분 쉼을 건다(backend/live_session.py).
    reaper = LiveSessionReaper(get_supabase_client, close_camera_session)
    reaper.start()
    # 라이브 직접 녹화(2026-10-20): 24시간 넘은 pending 행·R2 객체 + 6시간마다 고아 객체 정리(backend/recording_cleanup.py).
    recording_reaper = PendingRecordingReaper(get_supabase_client, delete_object,
                                              list_fn=list_objects)
    recording_reaper.start()
    try:
        yield
    finally:
        recording_reaper.stop()
        reaper.stop()
        relay.stop()


app = FastAPI(
    title="terra-server",
    description=_APP_DESCRIPTION,
    version="0.1.0",
    openapi_tags=tags_metadata,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """/recordings 의 요청 형식 오류는 400 + 문자열 detail (앱은 400 만 비재시도).

    다른 경로는 기존 계약 그대로 FastAPI 기본 422.
    """
    if request.url.path.startswith("/recordings"):
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = first.get("msg", "invalid")
        return JSONResponse(status_code=400, content={"detail": f"invalid request: {loc}: {msg}"})
    return await request_validation_exception_handler(request, exc)


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


# 운영 콘솔(beta.html·live.html) 접근 판정. 정적 HTML 은 누구나 받지만 페이지가 부팅 때 이걸 물어 로그인·권한 없으면 막는다.
# CONSOLE_ADMIN_EMAILS(쉼표 목록)가 비어 있으면 로그인한 아무 계정이나 통과(restricted=false) — 운영에선 채울 것.
# 진짜 보호는 각 API 의 JWT 소유자 검사이고, 이건 "테스터가 자기 계정으로 운영 페이지를 여는 것"을 막는 문.
def _console_admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv("CONSOLE_ADMIN_EMAILS", "").split(",") if e.strip()}


@app.get("/console/access", include_in_schema=False)
def _console_access(
    user_id: str = Depends(get_current_user_id),
    authorization: str | None = Header(default=None),
) -> dict:
    email: str | None = None
    if authorization and os.getenv("AUTH_MODE", "dev").strip().lower() == "prod":
        try:
            email = (get_jwt_payload(authorization=authorization).get("email") or "").strip().lower() or None
        except Exception:  # noqa: BLE001 — 이미 get_current_user_id 가 검증했으니 email 만 못 읽은 것
            email = None
    allowed = _console_admin_emails()
    if not allowed:
        return {"admin": True, "restricted": False, "email": email, "user_id": user_id}
    return {"admin": bool(email and email in allowed), "restricted": True, "email": email, "user_id": user_id}


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
app.include_router(webrtc.router)
app.include_router(clips.camera_clips_router)
app.include_router(clips.enclosure_clips_router)
app.include_router(clips.clips_router)
app.include_router(commands.router)
app.include_router(lcd.router)
app.include_router(schedules.device_schedules_router)
app.include_router(schedules.schedules_router)
app.include_router(settings.router)
app.include_router(firmware.router)
app.include_router(recordings.router)
app.include_router(ops.router)

# 정적 웹 콘솔 — 루트(/) 에 마운트.
# 라우터들 다음에 등록해야 /devices, /cameras, /web-config 등이 우선 매칭됨.
# html=True 면 / 요청 시 index.html 자동 서빙.
_WEB_DIR = REPO_ROOT / "web"


class _NoCacheStaticFiles(StaticFiles):
    """콘솔 정적 파일에 Cache-Control: no-cache.

    왜: 2026-10-21 beta.html 은 새로 받았는데 shared.js 는 브라우저 휴리스틱 캐시(명시 헤더 없을 때 Last-Modified
    기준으로 알아서 오래 보관)가 옛 파일을 써서 "liveBadgeHtml is not defined" 로 깨졌다. no-cache 는 "쓰기 전에
    매번 서버에 물어봐라" — StaticFiles 가 ETag/Last-Modified 를 주므로 안 바뀐 파일은 304 로 싸게 끝난다.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if _WEB_DIR.is_dir():
    app.mount("/", _NoCacheStaticFiles(directory=_WEB_DIR, html=True), name="web")
