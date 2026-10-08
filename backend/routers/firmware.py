"""
펌웨어 릴리스·OTA 작업 라우터 — Stage J (specs/stage-j-ota.md).

엔드포인트:
- GET /firmware/releases                 — 릴리스 목록 (JWT, 콘솔 드롭다운용)
- GET /firmware/jobs                     — 본인 기기의 OTA 작업 목록 (JWT)
- GET /firmware/jobs/{job_id}            — 작업 1건 (JWT, 본인 기기만)
- GET /firmware/jobs/{job_id}/bin        — **펌웨어가 받는 바이너리** (Bearer <camera/device token>)

작업 생성·apply 는 대상별 라우터에 있다:
- POST /cameras/{id}/ota, POST /cameras/{id}/ota/{job_id}/apply   (routers/cameras.py)
- POST /devices/{id}/ota, POST /devices/{id}/ota/{job_id}/apply   (routers/commands.py)

## 바이너리를 R2 직접이 아니라 서버가 프록시하는 이유
R2 anycast IP 하나(172.64.66.1)가 가정 회선(KT)에서 불통이고 펌웨어에 DNS 폴백이 없다
(2026-09-29 업로드 실패 원인). Lightsail → 기기 경로는 하트비트·페어링으로 검증돼 있다.
토큰 인증·다운로드 로그가 서버에 남고, 명령 페이로드도 짧아진다. 이그레스는 2MB × 20대.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend import ota_service
from backend.auth import get_current_user_id
from backend.auth_device import extract_bearer, verify_entity_token
from backend.r2_client import get_r2_bucket, get_r2_client
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/firmware", tags=["firmware"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "미존재 또는 본인 기기 작업이 아님"}}

# 다운로드를 허용하는 작업 상태. pending 도 허용 — 펌웨어가 ack 직후 바로 받으러 오면
# 서버가 ack 를 처리하기 전일 수 있다. ready 이후·종결 상태는 거절(URL 재사용 차단).
_DOWNLOADABLE = frozenset({"pending", "accepted", "downloading"})
_CHUNK = 64 * 1024


class ReleaseOut(BaseModel):
    id: str
    target: str
    version: str
    size_bytes: int
    sha256: str
    project_name: str | None = None
    idf_ver: str | None = None
    notes: str | None = None
    created_at: str
    retired_at: str | None = Field(None, description="설정돼 있으면 퇴역 — OTA 대상 불가 (include_retired=true 로만 보임)")
    retired_reason: str | None = None


class JobOut(BaseModel):
    id: str
    kind: str
    target_uuid: str
    release_id: str
    version: str | None = Field(None, description="릴리스 버전 (join)")
    status: str
    pct: int = 0
    prev_version: str | None = None
    error: str | None = None
    forced: bool = False
    created_at: str
    updated_at: str
    applied_at: str | None = None
    finished_at: str | None = None


def _pick(row: dict[str, Any], fields: Any) -> dict[str, Any]:
    """모델 필드만, None 은 빼서 기본값이 살게 (pct/forced 가 비어 온 행 대비)."""
    return {k: row[k] for k in fields if row.get(k) is not None}


def _release_out(r: dict[str, Any]) -> ReleaseOut:
    return ReleaseOut(**_pick(r, ReleaseOut.model_fields))


def _job_out(j: dict[str, Any], version: str | None) -> JobOut:
    data = _pick(j, [k for k in JobOut.model_fields if k != "version"])
    return JobOut(**data, version=version)


@router.get("/releases", response_model=list[ReleaseOut], summary="OTA 릴리스 목록",
            responses={**_AUTH_REQUIRED})
def list_releases(
    target: str | None = Query(None, description="camera_p4 | device_nano"),
    limit: int = Query(50, ge=1, le=200),
    include_retired: bool = Query(False, description="퇴역(retired_at 설정) 릴리스도 포함"),
    _user_id: str = Depends(get_current_user_id),
) -> list[ReleaseOut]:
    sb = get_supabase_client()
    q = sb.table("firmware_releases").select("*").order("created_at", desc=True).limit(limit)
    if target:
        q = q.eq("target", target)
    if not include_retired:
        q = q.is_("retired_at", "null")
    return [_release_out(r) for r in (q.execute().data or [])]


def _owned_entity_uuids(sb: Any, user_id: str) -> set[str]:
    ids: set[str] = set()
    for table in ("cameras", "devices"):
        res = sb.table(table).select("id").eq("owner_id", user_id).execute()
        ids |= {r["id"] for r in (res.data or [])}
    return ids


@router.get("/jobs", response_model=list[JobOut], summary="본인 기기의 OTA 작업 목록",
            responses={**_AUTH_REQUIRED})
def list_jobs(
    target_uuid: str | None = Query(None, description="cameras.id | devices.id 로 필터"),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
) -> list[JobOut]:
    sb = get_supabase_client()
    owned = _owned_entity_uuids(sb, user_id)
    if target_uuid is not None and target_uuid not in owned:
        raise HTTPException(status_code=404, detail="job not found")
    q = sb.table("ota_jobs").select("*").order("created_at", desc=True).limit(limit)
    if target_uuid:
        q = q.eq("target_uuid", target_uuid)
    elif owned:
        q = q.in_("target_uuid", sorted(owned))
    else:
        return []
    jobs = q.execute().data or []
    versions = _versions_for(sb, {j["release_id"] for j in jobs})
    return [_job_out(j, versions.get(j["release_id"])) for j in jobs]


def _versions_for(sb: Any, release_ids: set[str]) -> dict[str, str]:
    if not release_ids:
        return {}
    res = sb.table("firmware_releases").select("id, version").in_("id", sorted(release_ids)).execute()
    return {r["id"]: r["version"] for r in (res.data or [])}


@router.get("/jobs/{job_id}", response_model=JobOut, summary="OTA 작업 1건",
            responses={**_AUTH_REQUIRED, **_NOT_FOUND})
def get_job(job_id: str, user_id: str = Depends(get_current_user_id)) -> JobOut:
    sb = get_supabase_client()
    job = ota_service.get_job(sb, job_id)
    if not job or job["target_uuid"] not in _owned_entity_uuids(sb, user_id):
        raise HTTPException(status_code=404, detail="job not found")
    release = ota_service.get_release(sb, job["release_id"])
    return _job_out(job, release.get("version") if release else None)


def _iter_r2_object(key: str, size: int) -> Iterator[bytes]:
    """R2 객체를 청크로 읽어 넘긴다. 블로킹 I/O 라 `def` 라우터(스레드풀)에서만 호출."""
    client = get_r2_client()
    obj = client.get_object(Bucket=get_r2_bucket(), Key=key)
    body = obj["Body"]
    sent = 0
    try:
        for chunk in body.iter_chunks(_CHUNK):
            sent += len(chunk)
            yield chunk
    finally:
        body.close()
        if sent != size:
            logger.warning("firmware 스트림 조기 종료: key=%s sent=%d/%d", key, sent, size)


@router.get(
    "/jobs/{job_id}/bin",
    summary="OTA 바이너리 다운로드 (펌웨어용, Bearer <camera/device token>)",
    responses={
        401: {"description": "토큰 누락/불일치"},
        404: {"description": "작업 미존재"},
        409: {"description": "작업이 다운로드 가능한 상태가 아님"},
        410: {"description": "릴리스가 퇴역됨 (retired_at)"},
    },
    response_class=StreamingResponse,
)
def download_bin(job_id: str, authorization: str | None = Header(default=None)) -> StreamingResponse:
    """ota_prepare 명령의 `url` 로 펌웨어가 호출. 작업의 대상 기기 토큰이어야 한다.

    성공 시 `application/octet-stream` + `Content-Length` + `X-Firmware-Version` + `X-Firmware-SHA256`.
    첫 바이트를 보내기 전에 작업을 downloading 으로 표시한다(펌웨어 진행률 ack 가 덮어씀).
    """
    token = extract_bearer(authorization)
    sb = get_supabase_client()
    job = ota_service.get_job(sb, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    # 토큰 검증을 상태 검사보다 먼저 — 상태로 작업 존재/진행 정보를 흘리지 않는다.
    verify_entity_token(job["kind"], job["target_uuid"], token)
    if job["status"] not in _DOWNLOADABLE:
        raise HTTPException(status_code=409, detail=f"job is {job['status']}")
    release = ota_service.get_release(sb, job["release_id"])
    if not release:
        raise HTTPException(status_code=404, detail="release not found")
    if release.get("retired_at"):
        # 발행 뒤 퇴역된 경우. R2 객체도 지워져 있을 수 있으니 스트림 전에 끊는다.
        raise HTTPException(status_code=410, detail="release retired")

    if job["status"] != "downloading":
        try:
            ota_service.mark_downloading(sb, job_id, 0)
        except Exception:  # noqa: BLE001
            logger.exception("ota job %s downloading 표시 실패", job_id)
    logger.info("firmware download start: job=%s kind=%s target=%s version=%s size=%s",
                job_id, job["kind"], job["target_uuid"], release["version"], release["size_bytes"])

    size = int(release["size_bytes"])
    headers = {
        "Content-Length": str(size),
        "X-Firmware-Version": str(release["version"]),
        "X-Firmware-SHA256": str(release["sha256"]),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        _iter_r2_object(release["r2_key"], size),
        media_type="application/octet-stream",
        headers=headers,
        status_code=status.HTTP_200_OK,
    )


__all__ = ["router"]
