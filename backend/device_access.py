"""기기 접근 검사 — 본인 소유이고 해제되지 않은 기기만 통과.

라우터 4곳(commands/settings/lcd/schedules)이 같은 `_load_device_for_owner` 를 복붙하고
있었고, 소프트 해제(2026-09-16)로 `unlinked_at IS NULL` 조건이 추가되면서 한 곳으로 모았다.

규칙: 미존재 · 타인 소유 · 해제됨 → 전부 **404** (존재 여부 비노출, 기존 계약 유지).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def require_active_device(sb: Any, device_uuid: str, user_id: str) -> dict[str, Any]:
    """본인 소유 + 활성(unlinked_at IS NULL) device 행 반환. 아니면 404."""
    res = (
        sb.table("devices")
        .select("id, owner_id, device_id, unlinked_at, capabilities")
        .eq("id", device_uuid)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or row.get("owner_id") != user_id or row.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="device not found")
    return row


def require_active_camera(sb: Any, camera_uuid: str, user_id: str) -> dict[str, Any]:
    """본인 소유 + 활성 camera 행 반환. 아니면 404."""
    res = (
        sb.table("cameras")
        .select("id, owner_id, camera_id, unlinked_at")
        .eq("id", camera_uuid)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or row.get("owner_id") != user_id or row.get("unlinked_at"):
        raise HTTPException(status_code=404, detail="camera not found")
    return row


__all__ = ["require_active_camera", "require_active_device"]
