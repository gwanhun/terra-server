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
from zoneinfo import ZoneInfo

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

# R2 날짜 폴더 기준 타임존 — 사용자가 보는 날짜와 맞추기 위해 KST
KST = ZoneInfo("Asia/Seoul")

# R2 object key 패턴 (버킷 petcam-clips 를 petcam-lab 과 공유 → test/ prefix 로 분리):
#   비디오: "test/{camera_id}/{YYYY-MM-DD}/{HHMMSS}_{clip_id}.mp4"
#   썸네일: "test/{camera_id}/{YYYY-MM-DD}/{HHMMSS}_{clip_id}.jpg"
# 카메라별 폴더 → 날짜(UTC)별 폴더로 나누고, 파일명 앞에 시각(HHMMSS) prefix → 하루 내 정렬·식별 용이.
# cleanup 은 test/ prefix 스캔으로 동일 동작.
_KEY_RE = re.compile(
    r"^test/(?P<camera>[^/]+)/(?P<date>\d{4}-\d{2}-\d{2})/(?P<time>\d{6})_(?P<clip_id>[0-9a-f-]{36})\.(?P<ext>mp4|jpg)$"
)

# clip_purpose — 촬영 목적 계약 (production | test). motion_clips 에 DB 로 고정.
# 클라이언트 값을 신뢰하지 않고 서버가 r2_key prefix 에서 결정론적으로 도출한다.
# 라벨링 자격은 이 값과 별개다 (petcam-lab: purpose=production + canonical terra-clips/clips/
#   + system exclusion 없음 + media 존재 를 모두 요구). 여긴 촬영 목적만 판정.
CLIP_PURPOSE_TEST = "test"
CLIP_PURPOSE_PRODUCTION = "production"

# 목적별 허용 prefix (allowlist). `NOT LIKE 'test/%'` 로 열지 않는다 —
# 임의·오타 경로가 production 으로 새는 것을 막고 DB CHECK 와 정확히 동일하게 유지.
_TEST_PREFIXES = ("test/",)
_PRODUCTION_PREFIXES = (
    "terra-clips/clips/",      # canonical 운영 촬영 경로 (펌웨어 운영 승격 시 writer 연결)
    "research-quarantine/",    # 운영 촬영본의 연구 격리 disposition
    "research-excluded/",      # 운영 촬영본의 연구 제외 disposition
    "deleted/",                # 삭제 예정 disposition
)


def _derive_clip_purpose(key: str) -> str:
    """검증된 r2_key prefix 에서 clip_purpose 를 도출한다. 미지 prefix 는 400 (fail-closed)."""
    if key.startswith(_TEST_PREFIXES):
        return CLIP_PURPOSE_TEST
    if key.startswith(_PRODUCTION_PREFIXES):
        return CLIP_PURPOSE_PRODUCTION
    raise HTTPException(
        status_code=400,
        detail="r2_key prefix 가 허용 namespace 에 없음 (clip_purpose 도출 불가)",
    )

# VLM 하이라이트 억제셋 — 개체 프로파일 기반 상시 오탐 제거 (GET /clips/highlights).
#   shedding: 특정 개체(화이트 할리퀸 모프) 흰 체색을 밤 IR 에서 허물로 오인 → 100% 오탐 (2026-07 실측 30+건).
#   moving/unseen: 하이라이트 가치 낮음.
# TODO: 하드코딩 대신 개체별 설정 테이블로 (실제 탈피 영상 확보 시 shedding 해제 가능해야).
_VLM_SUPPRESSED_ACTIONS = ("moving", "unseen", "shedding")
_VLM_MIN_CONFIDENCE = 0.5
# care(건강 신호) vs enrichment(복지 활동) — 앱 2층 구성용.
_CARE_ACTIONS = frozenset({"hand_feeding", "drinking", "eating_paste", "eating_prey", "shedding"})
# behavior_labels 유효 action 화이트리스트 — 오타로 인한 GT 오염 방지 (POST /clips/{id}/labels).
_VALID_ACTIONS = frozenset({
    "moving", "hand_feeding", "drinking", "eating_paste",
    "eating_prey", "defecating", "shedding", "unseen",
})


# ---------- Pydantic ----------


class Highlight(BaseModel):
    """밤새 VLM 하이라이트 1건. behavior_logs(source='vlm') + motion_clips 조인 결과."""

    clip_id: str
    started_at: datetime
    thumbnail_key: str | None = None
    vlm_action: str
    confidence: float | None = None
    care_level: str = Field(..., description='"care"(건강) | "enrichment"(복지)')
    # null=미확인 | true=확인 | 정정된 action 문자열 (behavior_logs.verified/corrected_to 유래)
    user_confirmed: bool | str | None = None


class HighlightList(BaseModel):
    highlights: list[Highlight]


class LabelCreate(BaseModel):
    """하이라이트 사용자 확인/정정 요청 (behavior_labels UPSERT)."""

    action: str = Field(..., description="행동 클래스명 (화이트리스트 검증)")
    lick_target: str | None = Field(None, description="drinking 등에서 핥은 대상 (옵션)")
    note: str | None = None


class UploadUrlRequest(BaseModel):
    started_at: datetime = Field(..., description="ISO8601. 모션 감지 시작 시각.")
    duration_sec: float = Field(..., gt=0, le=600, examples=[10.0])
    with_thumbnail: bool = Field(
        default=True,
        description="true 면 비디오와 함께 썸네일 presigned PUT URL 도 발급. 펌웨어가 썸네일 안 쓰면 false.",
    )


class UploadUrlResponse(BaseModel):
    url: str = Field(..., description="비디오 R2 presigned PUT URL. Content-Type=video/mp4 로 PUT.")
    key: str = Field(..., description="비디오 R2 object key. 이후 POST /clips 호출 시 그대로 전달.")
    clip_id: str = Field(..., description="DB row 의 id 로 사용됨 (비디오 key 와 동일 UUID).")
    expires_in: int = Field(..., description="URL 유효 시간(초). 기본 300.")
    thumbnail_url: str | None = Field(
        None,
        description="썸네일 presigned PUT URL (with_thumbnail=true 일 때). Content-Type=image/jpeg.",
    )
    thumbnail_key: str | None = Field(
        None,
        description="썸네일 R2 key (비디오와 동일 prefix + .jpg). POST /clips 의 thumbnail_key 로 그대로 전달.",
    )


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
    clip_purpose: str | None = None
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


def _build_key(camera_id_text: str, clip_id: str, started_at: datetime, ext: str) -> str:
    """test/{camera_id}/{YYYY-MM-DD}/{HHMMSS}_{clip_id}.{ext} — ext 는 'mp4' 또는 'jpg'.

    카메라별 폴더 아래를 KST(Asia/Seoul) 날짜별 폴더로 나누고, 파일명 앞에 시각(HHMMSS)
    prefix 를 붙여 R2 목록에서 하루 내 시간순 정렬·식별이 쉽게 한다. 사용자가 보는 날짜와
    폴더 날짜를 일치시키기 위해 UTC 가 아닌 KST 기준. cleanup 은 'test/' prefix 스캔으로 동일.
    naive datetime 은 UTC 로 간주 후 변환 (워커가 UTC 로 보냄).
    """
    aware = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    ts = aware.astimezone(KST)
    date = ts.strftime("%Y-%m-%d")
    time = ts.strftime("%H%M%S")
    return f"test/{camera_id_text}/{date}/{time}_{clip_id}.{ext}"


def _parse_key(key: str, expected_camera_id_text: str, expected_ext: str) -> str:
    """key 검증 + clip_id 추출. 본인 카메라 prefix / 기대 ext 불일치는 400."""
    m = _KEY_RE.match(key)
    if not m:
        raise HTTPException(status_code=400, detail=f"잘못된 key 형식: {key}")
    cam_in_key = m.group("camera")
    ext_in_key = m.group("ext")
    if cam_in_key != expected_camera_id_text:
        raise HTTPException(
            status_code=400,
            detail=f"key 의 camera_id 가 본인 카메라와 다름 (expected={expected_camera_id_text}, got={cam_in_key})",
        )
    if ext_in_key != expected_ext:
        raise HTTPException(
            status_code=400,
            detail=f"key 의 확장자가 기대값과 다름 (expected=.{expected_ext}, got=.{ext_in_key})",
        )
    return m.group("clip_id")


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
    1. `url` 로 비디오 HTTPS PUT (body=mp4, Content-Type=video/mp4)
    2. (옵션) `thumbnail_url` 로 썸네일 HTTPS PUT (body=jpeg, Content-Type=image/jpeg)
    3. 성공 시 `POST /cameras/{id}/clips` 로 `key` + `thumbnail_key` 와 메타 등록

    `clip_id` 는 R2 object key 와 DB row id 가 같도록 미리 발급된 UUID.
    썸네일 key 는 비디오 key 의 확장자만 .jpg 로 바꾼 형태 (같은 prefix → R2 lifecycle align). URL TTL 5분.
    """
    clip_id = str(uuid.uuid4())
    key = _build_key(camera["camera_id"], clip_id, body.started_at, "mp4")

    try:
        url = generate_presigned_put_url(key, expires_in=DEFAULT_PUT_URL_TTL)
        thumbnail_key: str | None = None
        thumbnail_url: str | None = None
        if body.with_thumbnail:
            thumbnail_key = _build_key(camera["camera_id"], clip_id, body.started_at, "jpg")
            # 썸네일은 image/jpeg 로 서명해야 한다. 펌웨어가 Content-Type: image/jpeg 로 PUT 하므로
            # content_type 을 안 넘기면 기본값(video/mp4)으로 서명돼 서명 불일치 → R2 가 403 반환.
            thumbnail_url = generate_presigned_put_url(
                thumbnail_key, content_type="image/jpeg", expires_in=DEFAULT_PUT_URL_TTL
            )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("presigned PUT URL 발급 실패")
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc

    return UploadUrlResponse(
        url=url,
        key=key,
        clip_id=clip_id,
        expires_in=DEFAULT_PUT_URL_TTL,
        thumbnail_url=thumbnail_url,
        thumbnail_key=thumbnail_key,
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
    `thumbnail_key` 가 있으면 같은 검증 + clip_id 일치까지 확인.
    INSERT 성공 시 앱이 Realtime publication 으로 즉시 신규 클립 알림 수신.
    """
    clip_id = _parse_key(body.key, camera["camera_id"], "mp4")

    if body.thumbnail_key:
        thumb_clip_id = _parse_key(body.thumbnail_key, camera["camera_id"], "jpg")
        if thumb_clip_id != clip_id:
            raise HTTPException(
                status_code=400,
                detail=f"thumbnail_key 의 clip_id 가 key 와 불일치 (video={clip_id}, thumb={thumb_clip_id})",
            )

    payload: dict[str, Any] = {
        "id": clip_id,
        "camera_id": camera["id"],
        "enclosure_id": camera.get("enclosure_id"),
        "owner_id": camera["owner_id"],
        "started_at": body.started_at.isoformat(),
        "duration_sec": body.duration_sec,
        "r2_key": body.key,
        # 촬영 목적: 검증된 key prefix 에서 서버가 도출 (클라이언트 신뢰 안 함).
        # _parse_key 가 이미 ^test/ 를 강제하므로 현재는 항상 "test".
        "clip_purpose": _derive_clip_purpose(body.key),
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


@clips_router.get(
    "/{clip_id}/thumbnail/url",
    response_model=ClipUrl,
    summary="썸네일 표시용 presigned GET URL",
    responses={**_USER_AUTH, **_NOT_FOUND_CLIP, **_R2_ERROR},
)
def get_clip_thumbnail_url(
    clip_id: str,
    user_id: str = Depends(get_current_user_id),
) -> ClipUrl:
    """
    앱 클립 그리드가 썸네일 표시 직전 호출. 재생 URL 과 동일 인증/RLS (본인 카메라 클립만).

    재생 URL 과 로직 동일 — presign 대상만 `r2_key` → `thumbnail_key`.
    URL 자체가 단발 토큰 → `<img src>` 에 그대로 박을 수 있음.
    `thumbnail_key` 가 없는 클립은 404 (앱은 아이콘 폴백).
    """
    clip = _load_clip_for_owner(clip_id, user_id)

    thumbnail_key = clip.get("thumbnail_key")
    if not thumbnail_key:
        raise HTTPException(status_code=404, detail="thumbnail not found")

    try:
        url = generate_presigned_get_url(thumbnail_key, expires_in=DEFAULT_GET_URL_TTL)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("썸네일 presigned GET URL 발급 실패")
        raise HTTPException(status_code=502, detail=f"R2 error: {exc}") from exc

    return ClipUrl(url=url, expires_in=DEFAULT_GET_URL_TTL)


def _highlight_user_confirmed(vlm_action: str, label_action: str | None) -> bool | str | None:
    """본인 behavior_labels 기준 user_confirmed (GT SOT = behavior_labels).

    없음=None(미확인) | action==vlm=True(확인) | 다르면 정정된 action 문자열.
    """
    if label_action is None:
        return None
    return True if label_action == vlm_action else label_action


@clips_router.get(
    "/highlights",
    response_model=HighlightList,
    summary="밤새 VLM 하이라이트 (어젯밤 리포트)",
    responses={**_USER_AUTH},
)
def get_clip_highlights(
    since: datetime | None = Query(None, description="이 시각 이후 시작된 클립만 (ISO8601)"),
    limit: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
) -> HighlightList:
    """
    mac-mini 워커가 behavior_logs(source='vlm') 에 기록한 케어행동 하이라이트.
    억제셋(개체 프로파일 상시오탐)+저 confidence 제외 → 앱은 받은 것만 "AI 추정" 으로 표시.

    behavior_logs.clip_id ≡ motion_clips.id (미러 동일 UUID) → motion_clips INNER JOIN.
    미러 아닌 평가셋(source='upload') 로그는 motion_clips 에 없어 자동 배제.
    """
    sb = get_supabase_client()

    # 1) vlm 로그: 억제셋·confidence 필터. motion 조인은 2단계.
    q = (
        sb.table("behavior_logs")
        .select("clip_id, action, confidence")
        .eq("source", "vlm")
        .gte("confidence", _VLM_MIN_CONFIDENCE)
    )
    for action in _VLM_SUPPRESSED_ACTIONS:
        q = q.neq("action", action)
    logs = q.execute().data or []
    if not logs:
        return HighlightList(highlights=[])

    # clip 당 대표 로그 1개 (confidence 최고)
    by_clip: dict[str, dict[str, Any]] = {}
    for log in logs:
        cid = log.get("clip_id")
        if not cid:
            continue
        cur = by_clip.get(cid)
        if cur is None or (log.get("confidence") or 0) > (cur.get("confidence") or 0):
            by_clip[cid] = log

    # 2) motion_clips INNER JOIN 효과 + 본인 것만(service_role 이라 owner_id 명시 필터 필수) + since
    mq = (
        sb.table("motion_clips")
        .select("id, started_at, thumbnail_key")
        .eq("owner_id", user_id)
        .in_("id", list(by_clip.keys()))
    )
    if since is not None:
        mq = mq.gte("started_at", since.isoformat())
    clips = mq.order("started_at", desc=True).limit(limit).execute().data or []

    # 3) 본인 behavior_labels → user_confirmed (GT SOT = behavior_labels, 관리자 라벨웹과 단일화)
    final_ids = [c["id"] for c in clips]
    my_labels: dict[str, str] = {}
    if final_ids:
        lrows = (
            sb.table("behavior_labels")
            .select("clip_id, action")
            .eq("labeled_by", user_id)
            .in_("clip_id", final_ids)
            .execute()
            .data
            or []
        )
        my_labels = {r["clip_id"]: r["action"] for r in lrows}

    # 4) 조인 결과 합치기 (motion 에 있는 clip_id 만 남음 = INNER JOIN 효과)
    highlights = [
        Highlight(
            clip_id=clip["id"],
            started_at=clip["started_at"],
            thumbnail_key=clip.get("thumbnail_key"),
            vlm_action=by_clip[clip["id"]]["action"],
            confidence=by_clip[clip["id"]].get("confidence"),
            care_level="care" if by_clip[clip["id"]]["action"] in _CARE_ACTIONS else "enrichment",
            user_confirmed=_highlight_user_confirmed(
                by_clip[clip["id"]]["action"], my_labels.get(clip["id"])
            ),
        )
        for clip in clips
    ]
    return HighlightList(highlights=highlights)


@clips_router.post(
    "/{clip_id}/labels",
    status_code=status.HTTP_201_CREATED,
    summary="하이라이트 사용자 확인/정정 (GT 라벨)",
    responses={**_USER_AUTH, **_NOT_FOUND_CLIP},
)
def upsert_clip_label(
    clip_id: str,
    body: LabelCreate,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """
    앱 하이라이트 👍/👎/정정 → 사람 GT. behavior_labels 가 GT SOT (관리자 라벨웹과 단일화).
    유저당 clip 당 1행 UPSERT (재판정=갱신, 멱등).

    👍(맞음): action=vlm 예측 그대로 / 정정: action=사용자 선택.
    👎 만 누르고 정답 미선택 상태는 저장 안 함(호출 안 함) → user_confirmed 미확정 유지.
    """
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=422, detail=f"unknown action: {body.action}")
    _load_clip_for_owner(clip_id, user_id)  # 본인 소유 clip 만 라벨 (404 else)

    sb = get_supabase_client()
    row = {
        "clip_id": clip_id,
        "labeled_by": user_id,
        "action": body.action,
        "lick_target": body.lick_target,
        "note": body.note,
    }
    try:
        sb.table("behavior_labels").upsert(row, on_conflict="clip_id,labeled_by").execute()
    except Exception as exc:  # noqa: BLE001 — supabase-py 예외 타입 넓음
        logger.exception("behavior_labels upsert 실패 (clip=%s)", clip_id)
        raise HTTPException(status_code=502, detail=f"DB error: {exc}") from exc

    return {"ok": True, "clip_id": clip_id, "action": body.action}


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
