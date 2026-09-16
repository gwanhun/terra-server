"""기기 소프트 해제(unlink) — devices / cameras 공통 로직.

계약: 앱 회신 2026-09-16 §1 (docs/APP_DELIVERY_2026-09-16.md §1.3 후속).

  POST /devices/{uuid}/unlink · POST /cameras/{uuid}/unlink
  body { "request_id": "<앱 생성 UUID>" } → 200 { "id", "unlinked_at" }

동작 (앱 §1-2 순서):
  1. 소유권 확인
  2. unlinked_at = now(), enclosure_id = NULL, unlink_request_id 저장 — **행은 지우지 않는다**
  3. schedules.enabled = false (devices 만 — 해제된 기기의 예약이 실행되면 안 됨)
  4. 개체↔카메라 이력 종료 — 앱팀 DB 트리거가 2번 UPDATE 를 잡아 처리 (서버 무관)
  5. Mosquitto 계정 회수 — 이후 접속 거부
  6. 멱등 기록 = 2번의 unlink_request_id

멱등 (앱 §1-1 표):
  - 같은 request_id 재시도       → 200, 최초 응답 그대로
  - 이미 해제된 기기 + 다른 id   → 200, 기존 unlinked_at 그대로 (재해제 무해)
  - 미존재·타인 소유             → 404

## 트랜잭션에 대해
supabase-py 는 REST 단건 호출이라 2~5 를 한 트랜잭션으로 묶을 수 없다(재설계 회신 §4.3).
대신 **각 단계를 멱등하게** 만들고, 이미 해제된 기기에 대한 재호출에서도 3·5 를 다시
실행해 부분 실패를 스스로 메운다. 2번이 먼저 커밋되므로 그 이후 어느 단계가 실패해도
기기는 "해제됨" 상태이고, 재시도 한 번이면 나머지가 정리된다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException

from backend.mqtt import registry

logger = logging.getLogger(__name__)

EntityKind = Literal["devices", "cameras"]

# 테이블별 MQTT client_id 컬럼 (registry 회수용)
_KEY_COLUMN: dict[str, str] = {"devices": "device_id", "cameras": "camera_id"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unlink_entity(
    sb: Any,
    *,
    table: EntityKind,
    entity_uuid: str,
    user_id: str,
    request_id: str,
) -> dict[str, Any]:
    """소프트 해제 실행. `{"id", "unlinked_at"}` 반환. 미존재/타인 소유면 404."""
    key_col = _KEY_COLUMN[table]
    label = "device" if table == "devices" else "camera"

    # 2) 활성 행만 해제 — 조건부 UPDATE 라 동시 호출에도 한 번만 찍힌다
    res = (
        sb.table(table)
        .update({
            "unlinked_at": _now_iso(),
            "unlink_request_id": request_id,
            "enclosure_id": None,
        })
        .eq("id", entity_uuid)
        .eq("owner_id", user_id)
        .is_("unlinked_at", "null")
        .execute()
    )
    rows = res.data or []

    if rows:
        row = rows[0]
        first_time = True
    else:
        # 이미 해제됐거나(멱등 경로) 미존재/타인 소유(404)
        chk = (
            sb.table(table)
            .select(f"id, unlinked_at, {key_col}")
            .eq("id", entity_uuid)
            .eq("owner_id", user_id)
            .limit(1)
            .execute()
        )
        row = (chk.data or [None])[0]
        if not row or not row.get("unlinked_at"):
            raise HTTPException(status_code=404, detail=f"{label} not found")
        first_time = False

    # 3) 예약 비활성 (devices 만). 재호출에서도 실행 — 부분 실패 자가 복구.
    if table == "devices":
        try:
            sb.table("schedules").update({"enabled": False}).eq(
                "device_id", entity_uuid
            ).execute()
        except Exception:  # noqa: BLE001
            logger.exception("unlink: schedules 비활성 실패 (%s=%s)", label, entity_uuid)

    # 5) MQTT 계정 회수 — 실패해도 raise 하지 않음(운영 안정성, registry 관례와 동일)
    key = row.get(key_col)
    if key:
        registry.unregister_device(key)

    logger.info(
        "unlink %s=%s (%s) request_id=%s",
        label, entity_uuid, "신규" if first_time else "재호출", request_id,
    )
    return {"id": row["id"], "unlinked_at": row["unlinked_at"]}


__all__ = ["unlink_entity"]
