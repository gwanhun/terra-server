"""
LCD 라우터 — Stage I(커스텀 텍스트) + Stage N(화면보호기·전원·소등 시간대).

엔드포인트:
- POST   /devices/{device_uuid}/lcd         — 상단 밴드에 커스텀 텍스트 표시 (JWT)
- POST   /devices/{device_uuid}/lcd/clear   — 기본값("TERRA IOT")으로 복귀 (JWT)
- GET    /devices/{device_uuid}/lcd         — 서버가 확인한 LCD 상태 묶음 (Stage N)
- PUT    /devices/{device_uuid}/lcd/power   — 백라이트 on/off (lcd_power)
- PUT    /devices/{device_uuid}/lcd/quiet   — 소등 시간대 설정 (lcd_quiet) / DELETE 로 해제
- PUT    /devices/{device_uuid}/lcd/saver   — 화면보호기 교대 설정 (lcd_saver)
- POST   /devices/{device_uuid}/lcd/image   — 화면보호기 사진 업로드 (lcd_image) / DELETE 로 삭제
- GET    /devices/{device_uuid}/lcd/image/bin — 펌웨어가 받는 RGB565 (Bearer 기기 토큰, R2 스트리밍)

Stage N 명령은 펌웨어가 `capabilities.lcd_saver` 를 보고한 기기만 받는다(아니면 400). 규칙 자체는
기기 NVS 가 진실이고, 서버 `devices.lcd_*` 는 ACK ok 때 확정하는 "마지막 확인값" (handlers).

흐름: 서버가 텍스트를 1비트 비트맵으로 렌더 → `commands`(action='lcd_bitmap') 큐잉 →
dispatcher publish → 펌웨어가 base64 디코드 후 상단 밴드에 blit + NVS 저장(재부팅 유지).
mist 와 동일 파이프라인 재사용.

원문은 payload `lcd_text` 에 같이 남긴다(서버 전용 — dispatcher 가 발행에서 뺀다).
기기가 `result=ok` 로 ACK 하면 handlers 가 `devices.lcd_text` 로 확정 → 앱이 Realtime 으로 읽는다
(앱 요청 2026-10-06: 다른 폰·재설치 후에도 지금 LCD 문구를 보여주기).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import get_current_user_id
from backend.device_access import require_active_device
from backend.command_service import insert_pending_command
from backend.auth_device import DeviceAuthError, extract_bearer, verify_entity_token
from backend.lcd_render import (
    LCD_IMAGE_BYTES,
    LCD_SCREEN_H,
    LCD_SCREEN_W,
    MAX_IMAGE_BYTES,
    MAX_TEXT_LEN,
    InvalidImage,
    build_lcd_payload,
    render_image_rgb565,
)
from backend.ota_service import api_public_base_url
from backend.r2_client import (
    R2NotConfigured,
    delete_object,
    generate_presigned_get_url,
    get_r2_bucket,
    get_r2_client,
    list_objects,
    put_object_bytes,
)
from backend.scheduling import parse_time_of_day
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["lcd"])

_AUTH_REQUIRED = {401: {"description": "JWT 누락/검증 실패"}}
_NOT_FOUND = {404: {"description": "본인 디바이스가 아니거나 미존재"}}
_UNSUPPORTED = {400: {"description": "검증 실패 또는 펌웨어가 lcd_saver 미지원"}}

# LCD 명령 TTL — 비트맵 payload 라 발행 지연 여유를 좀 더 준다.
LCD_TTL_SEC = 30


class LcdTextRequest(BaseModel):
    text: str = Field(
        ...,
        max_length=MAX_TEXT_LEN,
        description=f"표시할 텍스트(한글 가능). 최대 {MAX_TEXT_LEN}자. 빈 문자열이면 기본값 복귀.",
        examples=["밥 6시"],
    )


class CommandOut(BaseModel):
    id: str
    action: str
    status: str




@router.post(
    "/{device_uuid}/lcd",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 커스텀 텍스트 표시",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def set_lcd_text(
    device_uuid: str,
    body: LcdTextRequest,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    """텍스트를 서버에서 비트맵으로 렌더 → 디바이스 상단 밴드에 표시. 빈 문자열이면 clear 처리."""
    sb = get_supabase_client()
    require_active_device(sb, device_uuid, user_id)

    if not body.text.strip():
        # 빈 텍스트 → 기본값 복귀
        return _insert(sb, device_uuid, user_id, "lcd_clear", None)

    payload = {**build_lcd_payload(body.text), "lcd_text": body.text}
    return _insert(sb, device_uuid, user_id, "lcd_bitmap", payload)


@router.post(
    "/{device_uuid}/lcd/clear",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 텍스트 초기화 (기본값 복귀)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def clear_lcd_text(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    require_active_device(sb, device_uuid, user_id)
    return _insert(sb, device_uuid, user_id, "lcd_clear", None)


def _insert(
    sb: Any, device_uuid: str, user_id: str, action: str, payload: dict | None
) -> CommandOut:
    inserted = insert_pending_command(
        sb,
        device_uuid=device_uuid,
        action=action,
        payload=payload,
        issued_by=user_id,
        ttl_sec=LCD_TTL_SEC,
    )
    if inserted is None:
        raise HTTPException(status_code=500, detail="command INSERT 실패")
    return CommandOut(id=inserted["id"], action=action, status="pending")


# ---------------------------------------------------------------------------
# Stage N — 화면보호기 / 전원 / 소등 시간대
# ---------------------------------------------------------------------------

# 서버는 KST 고정(scheduling.py 와 같은 전제). 해외 사용자가 생기면 devices.tz 로 바꿔 보내면 되고
# 펌웨어는 tz_offset_min 만 받으므로 수정 없음.
KST_OFFSET_MIN = 9 * 60

# 교대 주기 기본값 — 사용자 확인 전 임시 결정 2026-10-28 (이미지 20초 / 온습도 10초)
SAVER_IMAGE_SEC_DEFAULT = 20
SAVER_INFO_SEC_DEFAULT = 10
SAVER_SEC_MIN_IMAGE = 5
SAVER_SEC_MIN_INFO = 3
SAVER_SEC_MAX = 300

# devices 에서 앱/콘솔에 돌려주는 LCD 상태 컬럼 (lcd_text 포함)
_LCD_STATE_COLS = (
    "lcd_text, lcd_text_updated_at, lcd_power, lcd_quiet_start, lcd_quiet_end, "
    "lcd_saver, lcd_image_key, lcd_image_set_at, lcd_updated_at"
)

# R2 키: lcd/{device_uuid}/{sha8}.rgb565 (기기용 40KB) + .png (앱 미리보기). sha8 = RGB565 바이트의 sha256 앞 8자.
# ACK 확정값(devices.lcd_image_key)은 접두까지. 다운로드 URL 은 서버 경유 — 기기 토큰으로 소유 검증 + R2 anycast
# 블랙홀(2026-09-29)을 기기가 직접 안 만나게.
def lcd_image_key(device_uuid: str, sha8: str) -> str:
    return f"lcd/{device_uuid}/{sha8}"


def _preview_url(key: str | None) -> str | None:
    if not key:
        return None
    try:
        return generate_presigned_get_url(f"{key}.png")
    except R2NotConfigured:
        return None


class LcdPowerRequest(BaseModel):
    on: bool = Field(..., description="true=백라이트 켬, false=끔(소등 시간대와 무관하게 유지)")


class LcdQuietRequest(BaseModel):
    start: str = Field(..., description="소등 시작 'HH:MM' (KST)", examples=["23:00"])
    end: str = Field(..., description="소등 끝 'HH:MM' (KST). start 보다 이르면 자정 넘는 구간", examples=["07:00"])


class LcdSaverRequest(BaseModel):
    enabled: bool = Field(..., description="화면보호기 사용")
    mode: Literal["rotate", "image"] = Field(
        "rotate", description="rotate=사진 image_sec초 ↔ 온습도 info_sec초 교대, image=사진만(온습도 화면 없음)")
    image_sec: int = Field(SAVER_IMAGE_SEC_DEFAULT, ge=SAVER_SEC_MIN_IMAGE, le=SAVER_SEC_MAX,
                           description="이미지 화면 유지 초")
    info_sec: int = Field(SAVER_INFO_SEC_DEFAULT, ge=SAVER_SEC_MIN_INFO, le=SAVER_SEC_MAX,
                          description="온습도 화면 유지 초")
    show_offline_icon: bool = Field(True, description="이미지 중 서버 끊김이면 우상단 빨간 아이콘 표시")


class LcdImageOut(CommandOut):
    preview_url: str = Field(..., description="기기 화면과 같은 색(RGB565)의 160×128 PNG presigned URL — 사용자 확인용")
    sha256: str


class LcdStateOut(BaseModel):
    device_id: str
    lcd_text: str | None = None
    lcd_text_updated_at: str | None = None
    lcd_power: bool = True
    lcd_quiet_start: str | None = None
    lcd_quiet_end: str | None = None
    lcd_saver: dict[str, Any] | None = None
    lcd_image_key: str | None = None
    lcd_image_set_at: str | None = None
    lcd_updated_at: str | None = None
    preview_url: str | None = Field(None, description="확정된 사진의 미리보기 PNG presigned URL (있을 때)")

    model_config = ConfigDict(extra="ignore")


def _require_lcd_saver(device_row: dict[str, Any]) -> None:
    """펌웨어가 capabilities.lcd_saver 를 보고했는지. 아니면 400 — 구 펌웨어는 unknown_action 만 돌려준다."""
    caps = device_row.get("capabilities")
    if not (isinstance(caps, dict) and caps.get("lcd_saver") is True):
        raise HTTPException(
            status_code=400,
            detail="unsupported_firmware: 이 기기 펌웨어는 LCD 화면보호기/전원 제어를 지원하지 않음 (OTA 필요)",
        )


def _hhmm_to_min(value: str, name: str) -> int:
    try:
        t = parse_time_of_day(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"{name}: {e}") from e
    return t.hour * 60 + t.minute


@router.get(
    "/{device_uuid}/lcd",
    response_model=LcdStateOut,
    summary="LCD 상태 조회 (서버가 마지막으로 확인한 값)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND},
)
def get_lcd_state(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> LcdStateOut:
    sb = get_supabase_client()
    require_active_device(sb, device_uuid, user_id)
    res = sb.table("devices").select(_LCD_STATE_COLS).eq("id", device_uuid).limit(1).execute()
    row = (res.data or [None])[0] or {}
    return LcdStateOut(device_id=device_uuid, preview_url=_preview_url(row.get("lcd_image_key")), **row)


@router.put(
    "/{device_uuid}/lcd/power",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 백라이트 on/off",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_UNSUPPORTED},
)
def set_lcd_power(
    device_uuid: str,
    body: LcdPowerRequest,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    _require_lcd_saver(require_active_device(sb, device_uuid, user_id))
    return _insert(sb, device_uuid, user_id, "lcd_power", {"on": body.on})


@router.put(
    "/{device_uuid}/lcd/quiet",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 소등 시간대 설정 (기기 로컬 시각으로 판정, 재부팅·오프라인 무관)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_UNSUPPORTED},
)
def set_lcd_quiet(
    device_uuid: str,
    body: LcdQuietRequest,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    _require_lcd_saver(require_active_device(sb, device_uuid, user_id))
    start_min = _hhmm_to_min(body.start, "start")
    end_min = _hhmm_to_min(body.end, "end")
    if start_min == end_min:
        raise HTTPException(status_code=400, detail="start 와 end 가 같음 (0분 구간). 해제는 DELETE")
    payload = {"start_min": start_min, "end_min": end_min, "tz_offset_min": KST_OFFSET_MIN}
    return _insert(sb, device_uuid, user_id, "lcd_quiet", payload)


@router.delete(
    "/{device_uuid}/lcd/quiet",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="LCD 소등 시간대 해제",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_UNSUPPORTED},
)
def clear_lcd_quiet(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    _require_lcd_saver(require_active_device(sb, device_uuid, user_id))
    return _insert(sb, device_uuid, user_id, "lcd_quiet_clear", None)


@router.put(
    "/{device_uuid}/lcd/saver",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="화면보호기 설정 (이미지 ↔ 온습도 교대)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_UNSUPPORTED},
)
def set_lcd_saver(
    device_uuid: str,
    body: LcdSaverRequest,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    _require_lcd_saver(require_active_device(sb, device_uuid, user_id))
    return _insert(sb, device_uuid, user_id, "lcd_saver", body.model_dump())


@router.post(
    "/{device_uuid}/lcd/image",
    response_model=LcdImageOut,
    status_code=status.HTTP_201_CREATED,
    summary="화면보호기 사진 업로드 (서버가 160×128 RGB565 로 변환 → R2 → 기기가 HTTP 로 받아 플래시 저장)",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_UNSUPPORTED,
               503: {"description": "R2 미설정"}},
)
def set_lcd_image(
    device_uuid: str,
    file: UploadFile = File(..., description=f"PNG/JPG 등 사진 (≤ {MAX_IMAGE_BYTES} B)"),
    user_id: str = Depends(get_current_user_id),
) -> LcdImageOut:
    """원본은 저장하지 않는다. 변환 결과(.rgb565)와 미리보기(.png)만 R2 에 두고, 명령 payload 엔
    서버 경유 다운로드 URL·크기·sha256 을 싣는다. 기기는 다운로드·검증·플래시 기록이 끝난 뒤 ack.
    같은 sha 가 이미 기기 플래시에 있으면 다운로드 없이 바로 ok(펌웨어 dedupe)."""
    sb = get_supabase_client()
    row = require_active_device(sb, device_uuid, user_id)
    _require_lcd_saver(row)
    raw = file.file.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail=f"이미지가 너무 큼 (> {MAX_IMAGE_BYTES} B)")
    try:
        rgb565, preview_png = render_image_rgb565(raw)
    except InvalidImage as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    sha256 = hashlib.sha256(rgb565).hexdigest()
    key = lcd_image_key(device_uuid, sha256[:8])
    try:
        put_object_bytes(f"{key}.rgb565", rgb565, content_type="application/octet-stream")
        put_object_bytes(f"{key}.png", preview_png, content_type="image/png")
        _cleanup_orphans(sb, device_uuid, keep={key})
        preview_url = generate_presigned_get_url(f"{key}.png")
    except R2NotConfigured as e:
        raise HTTPException(status_code=503, detail="R2 미설정 — 화면보호기 사진 업로드 불가") from e

    payload = {
        "url": f"{api_public_base_url()}/devices/{device_uuid}/lcd/image/bin?sha={sha256[:8]}",
        "size": LCD_IMAGE_BYTES,
        "sha256": sha256,
        "w": LCD_SCREEN_W,
        "h": LCD_SCREEN_H,
        "enc": "rgb565",
        "slot": 0,
    }
    out = _insert(sb, device_uuid, user_id, "lcd_image", payload)
    return LcdImageOut(**out.model_dump(), preview_url=preview_url, sha256=sha256)


def _inflight_image_keys(sb: Any, device_uuid: str) -> set[str]:
    """아직 끝나지 않은(pending/sent) lcd_image 명령이 가리키는 키 — 기기가 지금 내려받고 있거나 곧 받을 사진."""
    res = (
        sb.table("commands")
        .select("payload")
        .eq("device_id", device_uuid)
        .eq("action", "lcd_image")
        .in_("status", ["pending", "sent"])
        .execute()
    )
    keys: set[str] = set()
    for r in res.data or []:
        payload = r.get("payload")
        sha = payload.get("sha256") if isinstance(payload, dict) else None
        if isinstance(sha, str) and len(sha) == 64:
            keys.add(lcd_image_key(device_uuid, sha[:8]))
    return keys


def _cleanup_orphans(sb: Any, device_uuid: str, keep: set[str]) -> None:
    """기기 접두 아래에서 '지금 올리는 키'·'확정된 키'·'진행 중 명령의 키' 외의 객체 삭제 — 기기가 ack 안 해
    남은 찌꺼기 정리. 확정 키는 기기 플래시에 살아 있고 앱 미리보기가 가리키므로, 진행 중 키는 기기가 지금
    내려받는 중일 수 있으므로(연속 업로드 때 404 방지) 지우지 않는다. ACK 경로(MQTT 스레드)는 R2 를 안 부르니
    이전 확정 사진도 여기서만 치워진다. 조회·삭제 실패는 로그만 — 고아가 잠시 남는 쪽이 안전."""
    try:
        res = sb.table("devices").select("lcd_image_key").eq("id", device_uuid).limit(1).execute()
        cur = ((res.data or [None])[0] or {}).get("lcd_image_key")
        if cur:
            keep = keep | {cur}
        keep = keep | _inflight_image_keys(sb, device_uuid)
        for obj_key, _ in list_objects(f"lcd/{device_uuid}/"):
            prefix = obj_key.rsplit(".", 1)[0]
            if prefix not in keep:
                delete_object(obj_key)
    except R2NotConfigured:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("lcd 이미지 고아 객체 정리 실패 (device=%s)", device_uuid)


_BIN_CHUNK = 8192


def _iter_r2(key: str) -> Iterator[bytes]:
    """R2 객체 스트리밍. 블로킹 I/O 라 `def` 라우터(스레드풀)에서만."""
    client = get_r2_client()
    obj = client.get_object(Bucket=get_r2_bucket(), Key=key)
    body = obj["Body"]
    try:
        for chunk in body.iter_chunks(_BIN_CHUNK):
            yield chunk
    finally:
        body.close()


@router.get(
    "/{device_uuid}/lcd/image/bin",
    summary="화면보호기 사진 RGB565 다운로드 (펌웨어용, Bearer <device token>)",
    responses={401: {"description": "토큰 누락/불일치"}, 404: {"description": "해당 sha 의 사진 없음"}},
    response_class=StreamingResponse,
)
def download_lcd_image_bin(
    device_uuid: str,
    sha: str = Query(..., min_length=8, max_length=8, pattern="^[0-9a-f]{8}$", description="sha256 앞 8자"),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """lcd_image 명령 payload 의 `url`. 기기 토큰이 이 device 의 것이어야 한다(firmware.py download_bin 과 동일 검증).
    `application/octet-stream` + `Content-Length` 40960 + `X-Image-SHA256`."""
    token = extract_bearer(authorization)
    try:
        verify_entity_token("device", device_uuid, token)
    except DeviceAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    key = f"{lcd_image_key(device_uuid, sha)}.rgb565"
    try:
        client = get_r2_client()
        head = client.head_object(Bucket=get_r2_bucket(), Key=key)
    except R2NotConfigured as e:
        raise HTTPException(status_code=503, detail="R2 미설정") from e
    except Exception as e:  # noqa: BLE001 — boto ClientError(404 등)
        raise HTTPException(status_code=404, detail="image not found") from e
    size = int(head["ContentLength"])
    return StreamingResponse(
        _iter_r2(key),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size)},
    )


@router.delete(
    "/{device_uuid}/lcd/image",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    summary="화면보호기 이미지 삭제",
    responses={**_AUTH_REQUIRED, **_NOT_FOUND, **_UNSUPPORTED},
)
def clear_lcd_image(
    device_uuid: str,
    user_id: str = Depends(get_current_user_id),
) -> CommandOut:
    sb = get_supabase_client()
    _require_lcd_saver(require_active_device(sb, device_uuid, user_id))
    return _insert(sb, device_uuid, user_id, "lcd_image_clear", None)
