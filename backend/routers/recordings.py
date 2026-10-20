"""
라이브 직접 녹화 라우터 (Stage L, 2026-10-20).

앱이 라이브 화면에서 폰으로 직접 녹화한 영상(≤60초 mp4 + jpg)을 R2 에 올리고 관리한다.
계약 SOT: docs/BACKEND_HANDOFF_REPLY_LIVE_RECORDING_2026-10-20.md

엔드포인트 (전부 JWT):
- POST   /recordings                     — 등록 + presigned PUT URL (새 201 / 같은 client_id 200)
- POST   /recordings/{id}/complete       — R2 영상 존재·크기 확인 후 ready
- GET    /recordings/{id}/url            — 재생용 presigned GET URL
- GET    /recordings/{id}/thumbnail/url  — 썸네일 presigned GET URL
- DELETE /recordings/{id}                — 행 + R2 객체 삭제 (pending 포함)

목록은 REST 가 없다 — 앱이 Supabase 에서 직접 SELECT (RLS: 본인 행만).

## 왜 recording_id 를 INSERT 전에 서버가 만드나?
R2 키와 행 id 를 같게 두려고 (clips 와 같은 이유 — 키만 보고 행을 찾을 수 있다).

## 왜 핸들러가 전부 def?
supabase-py·boto3 가 블로킹 I/O 라 async 핸들러에서 부르면 이벤트 루프가 멈춘다.
def 핸들러는 FastAPI 가 스레드풀에서 돌린다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.r2_client import (
    DEFAULT_GET_URL_TTL,
    BotoCoreError,
    ClientError,
    delete_object,
    generate_presigned_get_url,
    generate_presigned_put_url,
    head_object_size,
)
from backend.recording_cleanup import PENDING_TTL, purge_one
from backend.routers.clips import ClipUrl
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["recordings"])

TABLE = "live_recordings"

RECORDING_PUT_URL_TTL = 1800          # 30분 — 60초·30MB 업로드를 느린 망에서도 마치게
RECORDING_QUOTA = 50                  # 계정당 pending + ready
MIN_DURATION_MS = 3000
MAX_DURATION_MS = 60000
MAX_VIDEO_BYTES = 30 * 1024 * 1024
MAX_THUMB_BYTES = 5 * 1024 * 1024

# INSERT 컬럼 화이트리스트 — has_thumbnail 은 생성 컬럼이라 넣으면 Postgres 가 거부한다.
_INSERT_COLUMNS = (
    "id", "user_id", "camera_id", "client_id", "status", "started_at", "duration_ms",
    "size_bytes", "width", "height", "r2_key", "thumbnail_key",
)

_USER_AUTH = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 녹화가 아니거나 미존재"}}
_R2_ERROR = {502: {"description": "R2 응답 실패 (재시도 가능)"}}


# ---------------------------------------------------------------- 모델


class RecordingCreate(BaseModel):
    # 범위 검사는 Field(ge/le) 가 아니라 핸들러에서 직접 — 앱은 400 만 비재시도.
    camera_id: uuid.UUID
    client_id: uuid.UUID = Field(..., description="앱이 녹화마다 만든 uuid (재호출 멱등 키)")
    started_at: datetime
    duration_ms: int
    size_bytes: int
    width: int | None = None
    height: int | None = None
    has_thumbnail: bool


class RecordingComplete(BaseModel):
    status: str = Field(..., description="항상 ready")


class RecordingUpload(BaseModel):
    recording_id: str
    status: str = Field(..., description="pending | ready")
    video_upload_url: str | None = Field(None, description="presigned PUT (Content-Type: video/mp4)")
    thumb_upload_url: str | None = Field(None, description="presigned PUT (Content-Type: image/jpeg)")
    expires_at: datetime | None = Field(None, description="업로드 URL 만료 시각. ready 면 null")


# ---------------------------------------------------------------- 헬퍼


def _build_key(user_id: str, camera_id: str, recording_id: str, ext: str) -> str:
    """terra-clips/recordings/{user_id}/{camera_id}/{recording_id}.{ext} — ext 는 'mp4'|'jpg'."""
    return f"terra-clips/recordings/{user_id}/{camera_id}/{recording_id}.{ext}"


def _first(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _load_recording_for_owner(sb: Any, recording_id: str, user_id: str) -> dict:
    """본인 녹화 1행. uuid 형식 아님·없음·남의 것 → 404 "recording not found"."""
    try:
        rid = str(uuid.UUID(recording_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="recording not found") from exc
    res = (sb.table(TABLE).select("*")
           .eq("id", rid).eq("user_id", user_id).limit(1).execute())
    row = _first(res.data)
    if row is None:
        raise HTTPException(status_code=404, detail="recording not found")
    return row


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _is_stale_pending(row: dict[str, Any], now: datetime) -> bool:
    created = _parse_ts(row.get("created_at"))
    return row.get("status") == "pending" and created is not None and created < now - PENDING_TTL


def _put_urls(video_key: str, thumb_key: str | None) -> tuple[str, str | None]:
    try:
        video_url = generate_presigned_put_url(video_key, "video/mp4", RECORDING_PUT_URL_TTL)
        thumb_url = (generate_presigned_put_url(thumb_key, "image/jpeg", RECORDING_PUT_URL_TTL)
                     if thumb_key else None)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("recording presigned PUT URL 발급 실패")
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc
    return video_url, thumb_url


def _upload_response(row: dict[str, Any]) -> RecordingUpload:
    """기존 행 응답. ready → URL·expires_at 전부 null / pending → 저장된 키로 새 PUT URL."""
    if row.get("status") == "ready":
        return RecordingUpload(recording_id=row["id"], status="ready")
    video_url, thumb_url = _put_urls(row["r2_key"], row.get("thumbnail_key"))
    return RecordingUpload(
        recording_id=row["id"],
        status="pending",
        video_upload_url=video_url,
        thumb_upload_url=thumb_url,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=RECORDING_PUT_URL_TTL),
    )


def _existing_upload(sb: Any, user_id: str, client_id: str) -> RecordingUpload | None:
    """같은 (user_id, client_id) 행이 있으면 응답, 없으면(24시간 넘은 pending 을 지운 경우 포함) None."""
    for _ in range(2):    # 오래된 pending 삭제가 complete 와 엇갈리면 한 번 다시 읽는다
        res = (sb.table(TABLE).select("*")
               .eq("user_id", user_id).eq("client_id", client_id).limit(1).execute())
        row = _first(res.data)
        if row is None:
            return None
        if not _is_stale_pending(row, datetime.now(timezone.utc)):
            return _upload_response(row)
        if purge_one(sb, row, delete_object):
            logger.info("recording %s: 24시간 넘은 pending 재호출 → 옛 행 삭제 후 새로 등록", row["id"])
            return None
    return None


def _validate_create(body: RecordingCreate) -> None:
    if not MIN_DURATION_MS <= body.duration_ms <= MAX_DURATION_MS:
        raise HTTPException(
            status_code=400,
            detail=f"duration_ms must be between {MIN_DURATION_MS} and {MAX_DURATION_MS}",
        )
    if not 1 <= body.size_bytes <= MAX_VIDEO_BYTES:
        raise HTTPException(
            status_code=400, detail=f"size_bytes must be between 1 and {MAX_VIDEO_BYTES}",
        )


# ---------------------------------------------------------------- POST /recordings


@router.post(
    "",
    response_model=RecordingUpload,
    status_code=status.HTTP_201_CREATED,
    summary="녹화 등록 + 업로드 URL 발급",
    responses={
        200: {"description": "같은 client_id 재호출 — 기존 녹화 (ready 면 URL null)"},
        400: {"description": "길이·크기 범위 위반, 필드 누락·형식 오류"},
        403: {"description": "본인 카메라 아님"},
        409: {"description": "한도 초과 {code: recording_quota_exceeded, used, limit}"},
        **_USER_AUTH,
        **_R2_ERROR,
    },
)
def create_recording(
    body: RecordingCreate,
    response: Response,
    user_id: str = Depends(get_current_user_id),
) -> RecordingUpload:
    """
    처리 순서(고정): ① 같은 client_id 행 → 있으면 200 (바디 무시) ② 카메라 소유 ③ 한도 ④ INSERT.
    동시 같은 client_id 는 UNIQUE(user_id, client_id) 23505 를 잡아 ① 로 다시 읽는다.
    """
    _validate_create(body)
    sb = get_supabase_client()
    client_id = str(body.client_id)

    existing = _existing_upload(sb, user_id, client_id)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    camera_id = str(body.camera_id)
    # unlinked_at 은 보지 않는다 — 녹화 뒤 카메라를 해제하고 나중에 올려도 받는다.
    cam = (sb.table("cameras").select("id")
           .eq("id", camera_id).eq("owner_id", user_id).limit(1).execute())
    if _first(cam.data) is None:
        raise HTTPException(status_code=403, detail="camera not found or not owned")

    used_res = sb.table(TABLE).select("id").eq("user_id", user_id).execute()
    used = len(used_res.data) if isinstance(used_res.data, list) else 0
    if used >= RECORDING_QUOTA:
        raise HTTPException(
            status_code=409,
            detail={"code": "recording_quota_exceeded", "used": used, "limit": RECORDING_QUOTA},
        )

    recording_id = str(uuid.uuid4())
    video_key = _build_key(user_id, camera_id, recording_id, "mp4")
    thumb_key = _build_key(user_id, camera_id, recording_id, "jpg") if body.has_thumbnail else None
    video_url, thumb_url = _put_urls(video_key, thumb_key)

    values: dict[str, Any] = {
        "id": recording_id,
        "user_id": user_id,
        "camera_id": camera_id,
        "client_id": client_id,
        "status": "pending",
        "started_at": body.started_at.isoformat(),
        "duration_ms": body.duration_ms,
        "size_bytes": body.size_bytes,
        "width": body.width,
        "height": body.height,
        "r2_key": video_key,
        "thumbnail_key": thumb_key,
    }
    payload = {k: values[k] for k in _INSERT_COLUMNS}
    try:
        sb.table(TABLE).insert(payload).execute()
    except APIError as exc:
        if exc.code != "23505":
            raise
        # 같은 client_id 가 거의 동시에 들어와 다른 요청이 먼저 INSERT 했다 → 그 행을 돌려준다.
        existing = _existing_upload(sb, user_id, client_id)
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return existing

    return RecordingUpload(
        recording_id=recording_id,
        status="pending",
        video_upload_url=video_url,
        thumb_upload_url=thumb_url,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=RECORDING_PUT_URL_TTL),
    )


# ---------------------------------------------------------------- POST /recordings/{id}/complete


def _head_size(key: str) -> int | None:
    try:
        return head_object_size(key)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("recording R2 HEAD 실패 key=%s", key)
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc


def _delete_quietly(key: str) -> None:
    try:
        delete_object(key)
    except (BotoCoreError, ClientError):
        logger.error("recording r2 delete failed key=%s", key)


@router.post(
    "/{recording_id}/complete",
    response_model=RecordingComplete,
    summary="업로드 완료 — R2 영상 존재·크기 확인 후 ready",
    responses={
        409: {"description": "{code: upload_missing} / {code: upload_missing, reason: size_mismatch}"},
        **_USER_AUTH,
        **_NOT_FOUND,
        **_R2_ERROR,
    },
)
def complete_recording(
    recording_id: str,
    user_id: str = Depends(get_current_user_id),
) -> RecordingComplete:
    """
    영상이 없거나 크기가 다르면 409 (앱은 처음부터 다시 업로드).
    썸네일이 없거나 MAX_THUMB_BYTES 초과면 **썸네일만 버리고** ready — 409 를 주면 앱이 영상까지
    다시 올리고, 초과 파일은 같은 걸 또 올려 무한 반복된다.
    """
    sb = get_supabase_client()
    row = _load_recording_for_owner(sb, recording_id, user_id)
    if row.get("status") == "ready":
        return RecordingComplete(status="ready")

    size = _head_size(row["r2_key"])
    if size is None:
        raise HTTPException(status_code=409, detail={"code": "upload_missing"})
    if size != row.get("size_bytes"):
        raise HTTPException(
            status_code=409, detail={"code": "upload_missing", "reason": "size_mismatch"},
        )

    thumb_key = row.get("thumbnail_key")
    drop_thumb = False
    if thumb_key:
        thumb_size = _head_size(thumb_key)
        drop_thumb = thumb_size is None or thumb_size > MAX_THUMB_BYTES

    update: dict[str, Any] = {"status": "ready"}
    if drop_thumb:
        update["thumbnail_key"] = None
    res = (sb.table(TABLE).update(update)
           .eq("id", row["id"]).eq("user_id", user_id).eq("status", "pending").execute())
    if not (isinstance(res.data, list) and res.data):
        # 0행 — 다른 요청이 먼저 ready 로 바꿨거나 그 사이 삭제됐다.
        again = _load_recording_for_owner(sb, row["id"], user_id)
        if again.get("status") == "ready":
            return RecordingComplete(status="ready")
        raise HTTPException(status_code=404, detail="recording not found")

    if drop_thumb and thumb_key:
        logger.info("recording %s: 썸네일 없음/초과 → 썸네일만 버림", row["id"])
        _delete_quietly(thumb_key)
    return RecordingComplete(status="ready")


# ---------------------------------------------------------------- GET url / DELETE


def _load_ready(sb: Any, recording_id: str, user_id: str) -> dict:
    row = _load_recording_for_owner(sb, recording_id, user_id)
    if row.get("status") != "ready":
        # 업로드가 안 끝난 녹화는 재생 대상이 아니다
        raise HTTPException(status_code=404, detail="recording not found")
    return row


def _get_url(key: str) -> ClipUrl:
    try:
        url = generate_presigned_get_url(key, expires_in=DEFAULT_GET_URL_TTL)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("recording presigned GET URL 발급 실패")
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc
    return ClipUrl(url=url, expires_in=DEFAULT_GET_URL_TTL)


@router.get(
    "/{recording_id}/url",
    response_model=ClipUrl,
    summary="재생용 presigned GET URL (ready 만)",
    responses={**_USER_AUTH, **_NOT_FOUND, **_R2_ERROR},
)
def get_recording_url(
    recording_id: str,
    user_id: str = Depends(get_current_user_id),
) -> ClipUrl:
    row = _load_ready(get_supabase_client(), recording_id, user_id)
    return _get_url(row["r2_key"])


@router.get(
    "/{recording_id}/thumbnail/url",
    response_model=ClipUrl,
    summary="썸네일 presigned GET URL (ready 만, 썸네일 없으면 404)",
    responses={**_USER_AUTH, **_NOT_FOUND, **_R2_ERROR},
)
def get_recording_thumbnail_url(
    recording_id: str,
    user_id: str = Depends(get_current_user_id),
) -> ClipUrl:
    row = _load_ready(get_supabase_client(), recording_id, user_id)
    thumbnail_key = row.get("thumbnail_key")
    if not thumbnail_key:
        raise HTTPException(status_code=404, detail="thumbnail not found")
    return _get_url(thumbnail_key)


@router.delete(
    "/{recording_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="녹화 삭제 (행 + R2 영상·썸네일, pending 포함)",
    responses={**_USER_AUTH, **_NOT_FOUND},
)
def delete_recording(
    recording_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """R2 삭제 실패는 로그만 남기고 행은 지운다 (객체가 아직 없어도 성공)."""
    sb = get_supabase_client()
    row = _load_recording_for_owner(sb, recording_id, user_id)
    for key in (row.get("r2_key"), row.get("thumbnail_key")):
        if key:
            _delete_quietly(key)
    sb.table(TABLE).delete().eq("id", row["id"]).eq("user_id", user_id).execute()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "MAX_DURATION_MS",
    "MAX_THUMB_BYTES",
    "MAX_VIDEO_BYTES",
    "MIN_DURATION_MS",
    "RECORDING_PUT_URL_TTL",
    "RECORDING_QUOTA",
    "RecordingComplete",
    "RecordingCreate",
    "RecordingUpload",
    "router",
]
