"""
관리자 라우터 (2026-10-27) — 운영 콘솔(web/beta.html·live.html)이 사용자 비밀번호 없이 전체 계정을 본다.

배경: 베타 초기엔 운영자가 만든 테스트 계정(공통 비밀번호)으로 콘솔이 계정마다 로그인해 JWT 를 받았다.
사용자가 직접 가입·기기 등록을 하게 되면서 그 방식이 막혔다 → 관리자 계정(ADMIN_USER_IDS) 한 번 로그인으로
(1) 여기서 전체 계정·기기·카메라를 한 번에 받고, (2) 개별 동작은 기존 API 에 `X-Act-As-User` 를 붙여 부른다
(backend/auth.py 대행 설명).

엔드포인트:
- GET /admin/accounts — 가입 계정 전부 + 각 계정의 활성(unlinked_at IS NULL) 기기·카메라. 관리자 JWT 만(403).

## 왜 service_role 로 auth.users 를 읽나
가입자 목록은 `auth.admin.list_users` 로만 볼 수 있다(공개 profiles 테이블 없음). devices/cameras 는
owner_id 로 묶는다 — RLS 바이패스지만 **관리자만** 통과시키므로 명시 필터가 필요 없다(이 라우터 전용 규칙).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.auth import require_admin
from backend.routers.cameras import _CAMERA_OUT_COLUMNS, CameraOut
from backend.routers.devices import DeviceOut
from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_FORBIDDEN = {403: {"description": "관리자 아님 — detail 에 호출자 user_id (ADMIN_USER_IDS 에 넣을 값)"}}
_DEVICE_COLUMNS = (
    "id, device_id, enclosure_id, name, species, firmware_ver, capabilities, sys_state, "
    "temp_offset_c, created_at, last_seen_at, is_online, owner_id"
)
_LIST_USERS_PAGE = 1000


class AdminAccountOut(BaseModel):
    user_id: str
    email: str | None = Field(None, description="auth.users.email. 계정 행이 없으면(삭제됨) null")
    created_at: str | None = Field(None, description="가입 시각")
    last_sign_in_at: str | None = None
    deleted_at: str | None = Field(None, description="소프트 삭제된 계정(로그인 불가). 기기가 남아 있으면 그대로 보인다")
    devices: list[DeviceOut]
    cameras: list[CameraOut]


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _list_all_users(sb: Any) -> list[Any]:
    """auth.admin.list_users 를 끝까지 넘긴다(한 페이지 1000)."""
    users: list[Any] = []
    page = 1
    while True:
        batch = sb.auth.admin.list_users(page=page, per_page=_LIST_USERS_PAGE) or []
        users.extend(batch)
        if len(batch) < _LIST_USERS_PAGE:
            return users
        page += 1


@router.get(
    "/accounts",
    response_model=list[AdminAccountOut],
    summary="전체 계정 + 등록 기기·카메라 (관리자)",
    responses={**_FORBIDDEN},
)
def list_accounts() -> list[AdminAccountOut]:
    """가입 계정 전부(기기 없는 계정 포함) + 각 계정의 활성 기기·카메라.

    정렬: 기기·카메라가 있는 계정 먼저, 그 안에서 가입 시각 내림차순. 계정 행이 지워졌는데 기기가 남은
    소유자(email null)는 맨 뒤. `token_hash` 류는 Out 모델에 없어 응답에 안 실린다.
    """
    sb = get_supabase_client()
    users = _list_all_users(sb)

    devs = (
        sb.table("devices").select(_DEVICE_COLUMNS).is_("unlinked_at", "null")
        .order("created_at", desc=True).execute()
    ).data or []
    cams = (
        sb.table("cameras").select(_CAMERA_OUT_COLUMNS + ", owner_id").is_("unlinked_at", "null")
        .order("created_at", desc=True).execute()
    ).data or []
    d_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    c_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in devs:
        d_by[str(d.get("owner_id"))].append(d)
    for c in cams:
        c_by[str(c.get("owner_id"))].append(c)

    out: list[AdminAccountOut] = []
    seen: set[str] = set()
    for u in users:
        uid = str(u.id)
        seen.add(uid)
        out.append(AdminAccountOut(
            user_id=uid, email=u.email, created_at=_iso(u.created_at),
            last_sign_in_at=_iso(u.last_sign_in_at), deleted_at=_iso(getattr(u, "deleted_at", None)),
            devices=[DeviceOut(**{k: v for k, v in d.items() if k != "owner_id"}) for d in d_by.get(uid, [])],
            cameras=[CameraOut.model_validate({k: v for k, v in c.items() if k != "owner_id"}) for c in c_by.get(uid, [])],
        ))
    for uid in sorted(set(d_by) | set(c_by)):
        if uid in seen:
            continue
        out.append(AdminAccountOut(
            user_id=uid, email=None, created_at=None, last_sign_in_at=None, deleted_at=None,
            devices=[DeviceOut(**{k: v for k, v in d.items() if k != "owner_id"}) for d in d_by[uid]],
            cameras=[CameraOut.model_validate({k: v for k, v in c.items() if k != "owner_id"}) for c in c_by[uid]],
        ))

    # 안정 정렬 두 번: 최신 가입 먼저 → 그 순서를 유지한 채 "등록 있음 / 등록 없음 / 계정 행 없음(맨 뒤)" 그룹으로
    out.sort(key=lambda a: a.created_at or "", reverse=True)
    out.sort(key=lambda a: (a.email is None, not (a.devices or a.cameras)))
    return out
