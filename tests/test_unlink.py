"""소프트 해제(unlink) — 서비스 로직 + REST 계약 + 접근 검사.

계약: 앱 회신 2026-09-16 §1 (docs/APP_DELIVERY_2026-09-16.md §1.3 후속)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import device_access, unlink_service
from tests.conftest import TEST_USER_ID

DEV = "aaaaaaaa-0000-4000-8000-000000000001"
CAM = "bbbbbbbb-0000-4000-8000-000000000002"
REQ = "cccccccc-0000-4000-8000-000000000003"
TS = "2026-09-16T03:00:00+00:00"


def _sb(
    *,
    update_rows: list[dict],
    select_row: dict | None,
    calls: dict[str, Any],
) -> MagicMock:
    """devices/cameras UPDATE·SELECT + schedules UPDATE 를 흉내내고 호출을 기록한다."""
    sb = MagicMock()

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name in ("devices", "cameras"):
            # update().eq().eq().is_().execute()
            upd = MagicMock()
            upd.eq.return_value.eq.return_value.is_.return_value.execute.return_value.data = update_rows
            t.update.side_effect = lambda patch: calls.setdefault("patch", []).append(patch) or upd
            # select().eq().eq().limit().execute()
            (
                t.select.return_value.eq.return_value.eq.return_value
                .limit.return_value.execute.return_value.data
            ) = [select_row] if select_row else []
        elif name == "schedules":
            def _upd(patch: dict) -> MagicMock:
                calls.setdefault("schedules", []).append(patch)
                c = MagicMock(); c.eq.return_value.execute.return_value.data = []
                return c
            t.update.side_effect = _upd
        return t

    sb.table.side_effect = _table
    return sb


@pytest.fixture(autouse=True)
def _no_registry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    unregistered: list[str] = []
    monkeypatch.setattr(
        unlink_service.registry, "unregister_device",
        lambda key: unregistered.append(key) or True,
    )
    return unregistered


# ---------- unlink_entity ----------


def test_first_unlink_marks_row_disables_schedules_revokes_mqtt(_no_registry: list[str]) -> None:
    calls: dict[str, Any] = {}
    sb = _sb(update_rows=[{"id": DEV, "unlinked_at": TS, "device_id": "terra-a1"}],
             select_row=None, calls=calls)

    out = unlink_service.unlink_entity(
        sb, table="devices", entity_uuid=DEV, user_id=TEST_USER_ID, request_id=REQ)

    assert out == {"id": DEV, "unlinked_at": TS}
    patch = calls["patch"][0]
    assert patch["enclosure_id"] is None            # 그룹 연결 해제
    assert patch["unlink_request_id"] == REQ         # 멱등 기록
    assert "unlinked_at" in patch
    assert calls["schedules"] == [{"enabled": False}]  # 예약 비활성
    assert _no_registry == ["terra-a1"]              # MQTT 회수


def test_already_unlinked_returns_existing_and_self_heals(_no_registry: list[str]) -> None:
    """이미 해제된 기기: 200 + 기존 unlinked_at. 예약 비활성·MQTT 회수는 다시 실행(부분 실패 복구)."""
    calls: dict[str, Any] = {}
    sb = _sb(update_rows=[],
             select_row={"id": DEV, "unlinked_at": TS, "device_id": "terra-a1"},
             calls=calls)

    out = unlink_service.unlink_entity(
        sb, table="devices", entity_uuid=DEV, user_id=TEST_USER_ID, request_id="other-req")

    assert out == {"id": DEV, "unlinked_at": TS}     # 최초 값 그대로
    assert calls["schedules"] == [{"enabled": False}]
    assert _no_registry == ["terra-a1"]


def test_not_found_or_foreign_is_404() -> None:
    calls: dict[str, Any] = {}
    sb = _sb(update_rows=[], select_row=None, calls=calls)
    with pytest.raises(HTTPException) as ei:
        unlink_service.unlink_entity(
            sb, table="devices", entity_uuid=DEV, user_id=TEST_USER_ID, request_id=REQ)
    assert ei.value.status_code == 404
    assert "schedules" not in calls


def test_active_row_without_unlinked_at_is_404_not_idempotent_path() -> None:
    """UPDATE 가 0행인데 SELECT 로는 활성으로 보이면(경합 등) 재해제 경로가 아니라 404."""
    calls: dict[str, Any] = {}
    sb = _sb(update_rows=[], select_row={"id": DEV, "unlinked_at": None, "device_id": "x"}, calls=calls)
    with pytest.raises(HTTPException):
        unlink_service.unlink_entity(
            sb, table="devices", entity_uuid=DEV, user_id=TEST_USER_ID, request_id=REQ)


def test_camera_unlink_skips_schedules(_no_registry: list[str]) -> None:
    calls: dict[str, Any] = {}
    sb = _sb(update_rows=[{"id": CAM, "unlinked_at": TS, "camera_id": "p4cam-1"}],
             select_row=None, calls=calls)
    out = unlink_service.unlink_entity(
        sb, table="cameras", entity_uuid=CAM, user_id=TEST_USER_ID, request_id=REQ)
    assert out["id"] == CAM
    assert "schedules" not in calls                 # 카메라엔 예약 없음
    assert _no_registry == ["p4cam-1"]


def test_schedule_disable_failure_does_not_block(
    _no_registry: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """예약 비활성이 터져도 해제 자체는 성공 응답 (다음 재호출이 메움)."""
    calls: dict[str, Any] = {}
    sb = _sb(update_rows=[{"id": DEV, "unlinked_at": TS, "device_id": "terra-a1"}],
             select_row=None, calls=calls)
    orig = sb.table.side_effect

    def _boom(name: str) -> MagicMock:
        t = orig(name)
        if name == "schedules":
            t.update.side_effect = RuntimeError("db down")
        return t

    sb.table.side_effect = _boom
    out = unlink_service.unlink_entity(
        sb, table="devices", entity_uuid=DEV, user_id=TEST_USER_ID, request_id=REQ)
    assert out["id"] == DEV
    assert _no_registry == ["terra-a1"]              # 다음 단계는 계속


# ---------- REST ----------


def test_post_unlink_device_returns_id_and_time(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake(sb: Any, **kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"id": DEV, "unlinked_at": TS}

    monkeypatch.setattr("backend.routers.devices.unlink_entity", _fake)

    res = app_client.post(f"/devices/{DEV}/unlink", json={"request_id": REQ})
    assert res.status_code == 200, res.text
    assert res.json() == {"id": DEV, "unlinked_at": TS}
    assert captured["table"] == "devices"
    assert captured["user_id"] == TEST_USER_ID
    assert captured["request_id"] == REQ


def test_post_unlink_camera_routes_to_cameras_table(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "backend.routers.cameras.unlink_entity",
        lambda sb, **kw: captured.update(kw) or {"id": CAM, "unlinked_at": TS},
    )
    res = app_client.post(f"/cameras/{CAM}/unlink", json={"request_id": REQ})
    assert res.status_code == 200, res.text
    assert captured["table"] == "cameras"


def test_post_unlink_requires_uuid_request_id(app_client: TestClient, fake_sb: MagicMock) -> None:
    res = app_client.post(f"/devices/{DEV}/unlink", json={"request_id": "not-a-uuid"})
    assert res.status_code == 422
    res = app_client.post(f"/devices/{DEV}/unlink", json={})
    assert res.status_code == 422


def test_post_unlink_404_propagates(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _nf(sb: Any, **kw: Any) -> dict[str, Any]:
        raise HTTPException(status_code=404, detail="device not found")

    monkeypatch.setattr("backend.routers.devices.unlink_entity", _nf)
    res = app_client.post(f"/devices/{DEV}/unlink", json={"request_id": REQ})
    assert res.status_code == 404


# ---------- 활성 기기 접근 검사 ----------


def _sb_row(row: dict | None) -> MagicMock:
    sb = MagicMock()
    (
        sb.table.return_value.select.return_value.eq.return_value
        .limit.return_value.execute.return_value.data
    ) = [row] if row else []
    return sb


def test_require_active_device_ok() -> None:
    row = {"id": DEV, "owner_id": TEST_USER_ID, "device_id": "terra-a1", "unlinked_at": None}
    assert device_access.require_active_device(_sb_row(row), DEV, TEST_USER_ID) == row


@pytest.mark.parametrize(
    "row",
    [
        None,                                                                       # 미존재
        {"id": DEV, "owner_id": "someone-else", "unlinked_at": None},               # 타인
        {"id": DEV, "owner_id": TEST_USER_ID, "unlinked_at": TS},                   # 해제됨
    ],
)
def test_require_active_device_404(row: dict | None) -> None:
    with pytest.raises(HTTPException) as ei:
        device_access.require_active_device(_sb_row(row), DEV, TEST_USER_ID)
    assert ei.value.status_code == 404


def test_require_active_camera_unlinked_404() -> None:
    row = {"id": CAM, "owner_id": TEST_USER_ID, "camera_id": "p4cam-1", "unlinked_at": TS}
    with pytest.raises(HTTPException):
        device_access.require_active_camera(_sb_row(row), CAM, TEST_USER_ID)


# ---------- 해제된 기기는 목록·단건·수정에서 사라진다 ----------


def test_get_unlinked_device_is_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = {
        "id": DEV, "device_id": "terra-a1", "enclosure_id": None, "name": "x",
        "species": None, "firmware_ver": None, "capabilities": None,
        "created_at": TS, "last_seen_at": None, "is_online": False,
        "owner_id": TEST_USER_ID, "unlinked_at": TS,
    }
    assert app_client.get(f"/devices/{DEV}").status_code == 404


def test_list_devices_filters_unlinked(app_client: TestClient, fake_sb: MagicMock) -> None:
    """목록 쿼리가 unlinked_at IS NULL 조건을 건다 — 조건 유무를 체인 호출로 확인."""
    q = fake_sb.table.return_value.select.return_value.eq.return_value
    q.is_.return_value.order.return_value.execute.return_value.data = []
    assert app_client.get("/devices").status_code == 200
    q.is_.assert_called_once_with("unlinked_at", "null")
