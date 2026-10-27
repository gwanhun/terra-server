"""
관리자 라우터 (2026-10-27) — 운영 콘솔(web/beta.html·live.html)이 사용자 비밀번호 없이 전체 계정을 본다.

배경: 베타 초기엔 운영자가 만든 테스트 계정(공통 비밀번호)으로 콘솔이 계정마다 로그인해 JWT 를 받았다.
사용자가 직접 가입·기기 등록을 하게 되면서 그 방식이 막혔다 → 관리자 계정(ADMIN_USER_IDS) 한 번 로그인으로
(1) 여기서 전체 계정·기기·카메라를 한 번에 받고, (2) 개별 동작은 기존 API 에 `X-Act-As-User` 를 붙여 부른다
(backend/auth.py 대행 설명).

엔드포인트:
- GET /admin/accounts — 가입 계정 전부 + 각 계정의 활성(unlinked_at IS NULL) 기기·카메라. 관리자 JWT 만(403).
- PATCH /admin/accounts/{user_id} — 계정 그룹 표시(`group`: "beta" | null). 콘솔의 베타 테스터 / 그 외 필터용.

## 그룹 표시는 왜 auth app_metadata 인가 (2026-10-27)
"베타 테스터만 보기" 를 관리자 8명이 같은 기준으로 봐야 하니 브라우저 localStorage 는 안 된다. 새 테이블 대신
`auth.users.raw_app_meta_data.group` 에 둔다 — 마이그레이션 없음, 서버(service_role)만 쓸 수 있어 사용자가 못 바꿈,
list_users 응답에 같이 실려 추가 조회가 없다. 앱 JWT 에도 `app_metadata.group` 으로 들어가지만 앱은 안 읽는다.

## 왜 service_role 로 auth.users 를 읽나
가입자 목록은 `auth.admin.list_users` 로만 볼 수 있다(공개 profiles 테이블 없음). devices/cameras 는
owner_id 로 묶는다 — RLS 바이패스지만 **관리자만** 통과시키므로 명시 필터가 필요 없다(이 라우터 전용 규칙).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import require_admin
from backend.db_helpers import parse_uuid_or_404
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
ACCOUNT_GROUPS = ("beta",)          # 콘솔이 쓰는 값. 늘리면 web/beta.html 필터도 같이


class AdminAccountOut(BaseModel):
    user_id: str
    email: str | None = Field(None, description="auth.users.email. 계정 행이 없으면(삭제됨) null")
    created_at: str | None = Field(None, description="가입 시각")
    last_sign_in_at: str | None = None
    deleted_at: str | None = Field(None, description="소프트 삭제된 계정(로그인 불가). 기기가 남아 있으면 그대로 보인다")
    group: str | None = Field(None, description='관리자가 붙인 그룹 표시. "beta" = 베타 테스터, null = 그 외 (auth app_metadata.group)')
    devices: list[DeviceOut]
    cameras: list[CameraOut]


class AccountGroupIn(BaseModel):
    group: str | None = Field(None, description='"beta" 로 표시, null 로 해제')


def _group_of(user: Any) -> str | None:
    meta = getattr(user, "app_metadata", None) or {}
    g = meta.get("group") if isinstance(meta, dict) else None
    return g if isinstance(g, str) and g else None


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
            group=_group_of(u),
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


@router.patch(
    "/accounts/{user_id}",
    summary="계정 그룹 표시 (beta | null)",
    responses={**_FORBIDDEN, 400: {"description": "group 값이 허용 목록 밖"}, 404: {"description": "계정 없음"}},
)
def set_account_group(user_id: str, body: AccountGroupIn) -> dict[str, Any]:
    """`auth.users.app_metadata.group` 을 바꾼다. 다른 app_metadata 키는 그대로 둔다(병합)."""
    uid = parse_uuid_or_404(user_id, "account not found")
    if body.group is not None and body.group not in ACCOUNT_GROUPS:
        raise HTTPException(status_code=400, detail=f"group 은 {list(ACCOUNT_GROUPS)} 또는 null")
    sb = get_supabase_client()
    try:
        current = sb.auth.admin.get_user_by_id(uid)
    except Exception as exc:  # noqa: BLE001 — gotrue 는 없는 uid 를 예외로 준다
        raise HTTPException(status_code=404, detail="account not found") from exc
    user = getattr(current, "user", current)
    if user is None:
        raise HTTPException(status_code=404, detail="account not found")
    meta = dict(getattr(user, "app_metadata", None) or {})
    if body.group is None:
        meta.pop("group", None)
    else:
        meta["group"] = body.group
    sb.auth.admin.update_user_by_id(uid, {"app_metadata": meta})
    logger.info("admin account group user=%s group=%s", uid, body.group)
    return {"user_id": uid, "group": body.group}
