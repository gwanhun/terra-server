"""
모션 클립 라우터.

엔드포인트:
- POST   /cameras/{camera_uuid}/clips/upload-url   — R2 presigned PUT URL (Camera Token)
- POST   /cameras/{camera_uuid}/clips              — 업로드 완료 후 메타 등록 (Camera Token)
- GET    /enclosures/{enclosure_id}/clips          — 사육장의 클립 목록 (JWT)
- GET    /clips/{clip_id}/url                       — 재생용 presigned GET URL (JWT)
- DELETE /clips/{clip_id}                           — 클립 삭제 + R2 객체 삭제 (JWT)

흐름:
1. 워커: POST .../clips/upload-url → { url, key, clip_id, expires_in }
2. 워커: HTTPS PUT <url> (body=mp4)
3. 워커: POST .../clips { key, started_at, duration_sec, ... } → INSERT with id=clip_id
4. 앱: GET /clips/{id}/url → presigned GET → 재생

## 왜 clip_id 를 1단계에서 미리 생성?
key 와 DB row id 를 동일하게 유지하면:
- 객체 키만 보고 DB row 즉시 조회 가능 (디버깅/cleanup 편함)
- 업로드 실패 시 R2 orphan + DB row 없음 → cleanup 스크립트가 prefix 스캔으로 처리

## 왜 별도 라우터 인스턴스 3개?
prefix 가 `/cameras`, `/enclosures`, `/clips` 셋 다 다름. APIRouter 는 prefix 1개만
지원하므로 인스턴스 분리. main.py 에서 셋 다 include.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import get_current_user_id
from backend.auth_camera import get_authed_camera
from backend.r2_client import (
    DEFAULT_GET_URL_TTL,
    DEFAULT_PUT_URL_TTL,
    BotoCoreError,
    ClientError,
    delete_object,
    generate_presigned_get_url,
    generate_presigned_put_url,
)
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

# 라우터 3개 — main.py 에서 각각 include
camera_clips_router = APIRouter(prefix="/cameras", tags=["clips"])
enclosure_clips_router = APIRouter(prefix="/enclosures", tags=["clips"])
clips_router = APIRouter(prefix="/clips", tags=["clips"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# R2 object key 패턴: "clips/{YYYY}/{MM}/{DD}/{camera_id}/{clip_id}.mp4"
_KEY_RE = re.compile(
    r"^clips/(\d{4})/(\d{2})/(\d{2})/([^/]+)/([0-9a-f-]{36})\.mp4$"
)


# ---------- Pydantic ----------


class UploadUrlRequest(BaseModel):
    started_at: datetime = Field(..., description="ISO8601. 모션 감지 시작 시각.")
    duration_sec: float = Field(..., gt=0, le=600, examples=[10.0])


class UploadUrlResponse(BaseModel):
    url: str = Field(..., description="R2 presigned PUT URL. Content-Type=video/mp4 로 PUT.")
    key: str = Field(..., description="R2 object key. 이후 POST /clips 호출 시 그대로 전달.")
    clip_id: str = Field(..., description="DB row 의 id 로 사용됨 (key 와 동일 UUID).")
    expires_in: int = Field(..., description="URL 유효 시간(초). 기본 300.")


class ClipMetaCreate(BaseModel):
    key: str = Field(..., description="upload-url 응답에서 받은 key 그대로")
    started_at: datetime
    duration_sec: float = Field(..., gt=0, le=600)
    file_size: int | None = Field(None, ge=0, description="bytes")
    width: int | None = Field(None, gt=0, examples=[1280])
    height: int | None = Field(None, gt=0, examples=[720])
    fps: float | None = Field(None, gt=0, examples=[24.0])
    codec: str = Field(default="h264", max_length=16)
    container: str = Field(default="mp4", max_length=8)
    thumbnail_key: str | None = Field(None, description="썸네일 R2 key (옵션)")
    motion_score: float | None = Field(None, ge=0.0, le=1.0, description="0~1, 감지 강도")


class ClipMetaCreated(BaseModel):
    id: str = Field(..., description="motion_clips.id (UUID)")


class ClipUrl(BaseModel):
    url: str = Field(..., description="R2 presigned GET URL")
    expires_in: int = Field(..., description="URL 유효 시간(초). 기본 3600.")


class ClipOut(BaseModel):
    id: str
    camera_id: str
    enclosure_id: str | None
    started_at: str
    duration_sec: float
    r2_key: str
    thumbnail_key: str | None
    file_size: int | None
    width: int | None
    height: int | None
    fps: float | None
    codec: str | None
    container: str | None
    motion_score: float | None
    created_at: str

    model_config = ConfigDict(extra="ignore")


class ClipList(BaseModel):
    items: list[ClipOut]
    count: int
    next_cursor: str | None = Field(
        None,
        description="다음 페이지의 cursor (마지막 항목의 started_at). null 이면 끝.",
    )
    has_more: bool


_CAMERA_AUTH = {401: {"description": "Camera Token 누락/검증 실패"}}
_USER_AUTH = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND_CLIP = {404: {"description": "본인 클립이 아니거나 미존재"}}
_BAD_KEY = {400: {"description": "key 포맷 불일치 또는 본인 카메라 prefix 가 아님"}}
_R2_ERROR = {502: {"description": "R2 응답 실패"}}


# ---------- 헬퍼 ----------


def _build_clip_key(camera_id_text: str, clip_id: str, started_at: datetime) -> str:
    """clips/{YYYY}/{MM}/{DD}/{camera_id}/{clip_id}.mp4"""
    ts = started_at.astimezone(timezone.utc) if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    return f"clips/{ts.year:04d}/{ts.month:02d}/{ts.day:02d}/{camera_id_text}/{clip_id}.mp4"


def _parse_clip_id_from_key(key: str, expected_camera_id_text: str) -> str:
    """key 검증 + clip_id 추출. 본인 카메라 prefix 가 아니면 400."""
    m = _KEY_RE.match(key)
    if not m:
        raise HTTPException(status_code=400, detail=f"잘못된 key 형식: {key}")
    cam_in_key = m.group(4)
    if cam_in_key != expected_camera_id_text:
        raise HTTPException(
            status_code=400,
            detail=f"key 의 camera_id 가 본인 카메라와 다름 (expected={expected_camera_id_text}, got={cam_in_key})",
        )
    return m.group(5)


def _load_clip_for_owner(clip_id: str, user_id: str) -> dict[str, Any]:
    """본인 소유 clip row 반환. 미존재/타 유저는 404."""
    sb = get_supabase_client()
    res = (
        sb.table("motion_clips")
        .select("*")
        .eq("id", clip_id)
        .single()
        .execute()
    )
    row = res.data
    if not row or row["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="clip not found")
    return row


# ---------- 워커(Camera Token) 엔드포인트 ----------


@camera_clips_router.post(
    "/{camera_id}/clips/upload-url",
    response_model=UploadUrlResponse,
    summary="R2 presigned PUT URL 발급 (워커용)",
    responses={**_CAMERA_AUTH, **_R2_ERROR},
)
def issue_upload_url(
    body: UploadUrlRequest,
    camera: dict[str, Any] = Depends(get_authed_camera),
) -> UploadUrlResponse:
    """
    워커가 모션 감지 후 호출. **Bearer 는 사용자 JWT 가 아니라 `camera_token`**.

    응답 후 워커는:
    1. `url` 로 HTTPS PUT (body=mp4, Content-Type=video/mp4)
    2. 성공 시 `POST /cameras/{id}/clips` 로 `key` 와 메타 등록

    `clip_id` 는 R2 object key 와 DB row id 가 같도록 미리 발급된 UUID. URL TTL 5분.
    """
    clip_id = str(uuid.uuid4())
    key = _build_clip_key(camera["camera_id"], clip_id, body.started_at)

    try:
        url = generate_presigned_put_url(key, expires_in=DEFAULT_PUT_URL_TTL)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("presigned PUT URL 발급 실패")
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc

    return UploadUrlResponse(
        url=url,
        key=key,
        clip_id=clip_id,
        expires_in=DEFAULT_PUT_URL_TTL,
    )


@camera_clips_router.post(
    "/{camera_id}/clips",
    response_model=ClipMetaCreated,
    status_code=status.HTTP_201_CREATED,
    summary="업로드 완료 후 motion_clips 메타 등록 (워커용)",
    responses={**_CAMERA_AUTH, **_BAD_KEY},
)
def create_clip_meta(
    body: ClipMetaCreate,
    camera: dict[str, Any] = Depends(get_authed_camera),
) -> ClipMetaCreated:
    """
    R2 PUT 완료 후 호출. `key` 는 `upload-url` 응답 그대로 전달.

    서버는 key 의 camera prefix 가 본인 카메라와 일치하는지 검증 → 불일치는 400.
    INSERT 성공 시 앱이 Realtime publication 으로 즉시 신규 클립 알림 수신.
    """
    clip_id = _parse_clip_id_from_key(body.key, camera["camera_id"])

    payload: dict[str, Any] = {
        "id": clip_id,
        "camera_id": camera["id"],
        "enclosure_id": camera.get("enclosure_id"),
        "owner_id": camera["owner_id"],
        "started_at": body.started_at.isoformat(),
        "duration_sec": body.duration_sec,
        "r2_key": body.key,
        "thumbnail_key": body.thumbnail_key,
        "file_size": body.file_size,
        "width": body.width,
        "height": body.height,
        "fps": body.fps,
        "codec": body.codec,
        "container": body.container,
        "motion_score": body.motion_score,
    }

    sb = get_supabase_client()
    res = sb.table("motion_clips").insert(payload).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="motion_clips INSERT 실패")
    return ClipMetaCreated(id=res.data[0]["id"])


# ---------- 사용자(JWT) 엔드포인트 ----------


@enclosure_clips_router.get(
    "/{enclosure_id}/clips",
    response_model=ClipList,
    summary="사육장의 모션 클립 목록 (cursor pagination)",
    responses={**_USER_AUTH, 404: {"description": "본인 사육장이 아니거나 미존재"}},
)
def list_enclosure_clips(
    enclosure_id: str,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="최대 200"),
    cursor: str | None = Query(
        None, description="이전 응답의 next_cursor (started_at ISO8601)"
    ),
    user_id: str = Depends(get_current_user_id),
) -> ClipList:
    """
    `started_at` 내림차순 + seek pagination.

    `next_cursor` 가 NULL 이 될 때까지 반복 호출 → 전체 목록 조회.
    """
    sb = get_supabase_client()

    # 본인 enclosure 확인
    enc = (
        sb.table("enclosures")
        .select("owner_id")
        .eq("id", enclosure_id)
        .single()
        .execute()
    )
    if not enc.data or enc.data["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="enclosure not found")

    q = (
        sb.table("motion_clips")
        .select("*")
        .eq("enclosure_id", enclosure_id)
        .eq("owner_id", user_id)
        .order("started_at", desc=True)
        .limit(limit + 1)
    )
    if cursor:
        q = q.lt("started_at", cursor)

    res = q.execute()
    rows = res.data or []
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = items[-1]["started_at"] if has_more and items else None

    return ClipList(
        items=[ClipOut.model_validate(r) for r in items],
        count=len(items),
        next_cursor=next_cursor,
        has_more=has_more,
    )


@clips_router.get(
    "/{clip_id}/url",
    response_model=ClipUrl,
    summary="영상 재생용 presigned GET URL",
    responses={**_USER_AUTH, **_NOT_FOUND_CLIP, **_R2_ERROR},
)
def get_clip_url(
    clip_id: str,
    user_id: str = Depends(get_current_user_id),
) -> ClipUrl:
    """
    앱이 영상 재생 직전 호출. URL TTL 1시간 — 그 안에 시크/재생 모두 가능.

    URL 자체가 단발 토큰 → `<video src>` 태그에 그대로 박을 수 있음 (Authorization 불필요).
    """
    clip = _load_clip_for_owner(clip_id, user_id)

    try:
        url = generate_presigned_get_url(clip["r2_key"], expires_in=DEFAULT_GET_URL_TTL)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("presigned GET URL 발급 실패")
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc

    return ClipUrl(url=url, expires_in=DEFAULT_GET_URL_TTL)


@clips_router.delete(
    "/{clip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="클립 삭제 (R2 객체 + DB 행)",
    responses={**_USER_AUTH, **_NOT_FOUND_CLIP},
)
def delete_clip(
    clip_id: str,
    user_id: str = Depends(get_current_user_id),
) -> None:
    """
    R2 객체(영상 + 썸네일)와 DB row 동시 삭제.

    R2 delete 실패해도 DB 삭제는 진행 (orphan 객체는 30일 lifecycle 로 자동 정리).
    """
    clip = _load_clip_for_owner(clip_id, user_id)

    # R2 먼저 (실패해도 DB 삭제는 진행 — 어차피 30일 lifecycle 로 정리됨)
    try:
        delete_object(clip["r2_key"])
        if clip.get("thumbnail_key"):
            delete_object(clip["thumbnail_key"])
    except (BotoCoreError, ClientError) as exc:
        logger.warning("R2 delete 실패 (DB 삭제는 계속 진행): %s", exc)

    sb = get_supabase_client()
    res = (
        sb.table("motion_clips")
        .delete()
        .eq("id", clip_id)
        .eq("owner_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="clip not found")
