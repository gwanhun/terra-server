"""관리자 API (2026-10-27) — GET /admin/accounts + X-Act-As-User 대행.

사용자가 직접 가입하면서 운영 콘솔이 테스터 비밀번호로 로그인하던 방식이 막혀 생긴 경로.
FakeSB 에 auth.admin.list_users 스텁을 얹어 쓴다. 관리자 판정은 ADMIN_USER_IDS 환경변수(DEV_USER_ID 와 비교).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import OTHER_USER_ID, TEST_USER_ID
from tests.fake_sb import FakeSB

THIRD_USER_ID = "33333333-3333-3333-3333-333333333333"
GONE_OWNER_ID = "44444444-4444-4444-4444-444444444444"   # 계정 행은 지워졌는데 기기가 남은 소유자
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _user(uid: str, email: str, created: datetime, deleted: datetime | None = None, group: str | None = None) -> Any:
    return SimpleNamespace(id=uid, email=email, created_at=created, last_sign_in_at=None, deleted_at=deleted,
                           app_metadata=({"group": group, "provider": "email"} if group else {"provider": "email"}))


def _device(owner: str, device_id: str, **over: Any) -> dict[str, Any]:
    row = {"id": f"dev-{device_id}", "device_id": device_id, "owner_id": owner, "name": device_id,
           "species": None, "firmware_ver": "1.0.0", "created_at": "2026-10-01T00:00:00+00:00",
           "last_seen_at": None, "is_online": False, "unlinked_at": None}
    row.update(over)
    return row


def _camera(owner: str, camera_id: str, **over: Any) -> dict[str, Any]:
    row = {"id": f"cam-{camera_id}", "camera_id": camera_id, "owner_id": owner, "name": camera_id,
           "enclosure_id": None, "model": "esp32-p4", "firmware_ver": "0.4.8", "resolution": None, "fps": None,
           "clip_sec": None, "created_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00",
           "last_seen_at": None, "is_online": True, "unlinked_at": None}
    row.update(over)
    return row


class _AdminSB(FakeSB):
    """FakeSB + auth.admin.list_users (페이지 넘김 포함)."""

    def __init__(self, users: list[Any], **tables: list[dict[str, Any]]) -> None:
        super().__init__(**tables)
        self._users = users
        self.pages: list[int] = []
        sb = self

        class _Admin:
            def list_users(self, page: int = 1, per_page: int = 1000) -> list[Any]:
                sb.pages.append(page)
                return sb._users[(page - 1) * per_page: page * per_page]

            def get_user_by_id(self, uid: str) -> Any:
                u = next((x for x in sb._users if x.id == uid), None)
                if u is None:
                    raise RuntimeError("User not found")
                return SimpleNamespace(user=u)

            def update_user_by_id(self, uid: str, attributes: dict[str, Any]) -> Any:
                u = next(x for x in sb._users if x.id == uid)
                if "app_metadata" in attributes:
                    u.app_metadata = dict(attributes["app_metadata"])
                return SimpleNamespace(user=u)

        self.auth = SimpleNamespace(admin=_Admin())


def _client(monkeypatch: pytest.MonkeyPatch, sb: Any, *, admins: str = TEST_USER_ID) -> TestClient:
    monkeypatch.setenv("ADMIN_USER_IDS", admins)
    from backend.routers import admin, commands, devices

    monkeypatch.setattr(admin, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(devices, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(commands, "get_supabase_client", lambda: sb)
    from backend.main import app

    return TestClient(app)


def _sb() -> _AdminSB:
    users = [
        _user(TEST_USER_ID, "admin@test.com", T0),
        _user(OTHER_USER_ID, "tester@test.com", T1, group="beta"),
        _user(THIRD_USER_ID, "nodevice@test.com", T1),
        _user("55555555-5555-5555-5555-555555555555", "old@test.com", T0, deleted=T1),
    ]
    return _AdminSB(
        users,
        devices=[_device(OTHER_USER_ID, "terra-aaaa"), _device(OTHER_USER_ID, "terra-gone", unlinked_at="2026-10-02T00:00:00+00:00"),
                 _device(GONE_OWNER_ID, "terra-orphan")],
        cameras=[_camera(OTHER_USER_ID, "p4cam-aaaa"), _camera(TEST_USER_ID, "p4cam-admin")],
        commands=[],
    )


# ── GET /admin/accounts ────────────────────────────────────────────────────

def test_accounts_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """관리자 목록에 없으면 403 — detail 에 호출자 uid(운영자가 .env 에 넣을 값)."""
    client = _client(monkeypatch, _sb(), admins=OTHER_USER_ID)
    res = client.get("/admin/accounts")
    assert res.status_code == 403, res.text
    assert TEST_USER_ID in res.json()["detail"]
    assert "ADMIN_USER_IDS" in res.json()["detail"]


def test_accounts_admin_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADMIN_EMAILS — prod 모드에서 JWT email 클레임으로 판정(대소문자 무시). 403 detail 에 이메일도 실린다."""
    from backend import auth

    monkeypatch.setenv("AUTH_MODE", "prod")
    monkeypatch.setattr(auth, "verify_jwt", lambda token: {"sub": TEST_USER_ID, "email": "Owner@Test.com"})
    client = _client(monkeypatch, _sb(), admins="")
    hdr = {"Authorization": "Bearer x"}
    res = client.get("/admin/accounts", headers=hdr)
    assert res.status_code == 403 and "Owner@Test.com" in res.json()["detail"]

    monkeypatch.setenv("ADMIN_EMAILS", "someone@test.com, owner@test.com")
    assert client.get("/admin/accounts", headers=hdr).status_code == 200
    assert client.get("/devices", headers={**hdr, "X-Act-As-User": OTHER_USER_ID}).status_code == 200


def test_accounts_unset_admin_list_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _sb(), admins="")
    assert client.get("/admin/accounts").status_code == 403


def test_accounts_groups_devices_and_cameras_by_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _sb())
    res = client.get("/admin/accounts")
    assert res.status_code == 200, res.text
    by = {a["user_id"]: a for a in res.json()}

    tester = by[OTHER_USER_ID]
    assert tester["email"] == "tester@test.com"
    assert [d["device_id"] for d in tester["devices"]] == ["terra-aaaa"]      # 해제된 기기 제외
    assert [c["camera_id"] for c in tester["cameras"]] == ["p4cam-aaaa"]
    assert "owner_id" not in tester["devices"][0] and "token_hash" not in tester["cameras"][0]
    assert tester["group"] == "beta" and by[THIRD_USER_ID]["group"] is None

    assert by[THIRD_USER_ID]["devices"] == [] and by[THIRD_USER_ID]["cameras"] == []   # 가입만 한 계정도 보임
    assert by["55555555-5555-5555-5555-555555555555"]["deleted_at"] is not None

    orphan = by[GONE_OWNER_ID]
    assert orphan["email"] is None and [d["device_id"] for d in orphan["devices"]] == ["terra-orphan"]


def test_accounts_sort_registered_first_then_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    """등록 있는 계정(최신 가입 먼저) → 등록 없는 계정 → 계정 행 없는 소유자."""
    client = _client(monkeypatch, _sb())
    ids = [a["user_id"] for a in client.get("/admin/accounts").json()]
    assert ids[:2] == [OTHER_USER_ID, TEST_USER_ID]           # 둘 다 등록 있음, tester(10/20) 가 admin(10/1) 보다 최신
    assert ids[-1] == GONE_OWNER_ID


def test_accounts_paginates_list_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """1000명 꽉 찬 페이지가 오면 다음 페이지를 더 읽는다."""
    from backend.routers import admin

    monkeypatch.setattr(admin, "_LIST_USERS_PAGE", 2)
    sb = _sb()
    client = _client(monkeypatch, sb)
    res = client.get("/admin/accounts")
    assert res.status_code == 200
    assert sb.pages == [1, 2, 3]     # 2명씩 두 페이지가 꽉 차서 빈 3페이지까지 확인
    assert len(res.json()) == 5      # 4 계정 + 고아 소유자 1


def test_set_account_group_merges_app_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """beta 표시 → app_metadata.group 만 바뀌고 provider 같은 다른 키는 유지. null 로 해제."""
    sb = _sb()
    client = _client(monkeypatch, sb)
    res = client.patch(f"/admin/accounts/{THIRD_USER_ID}", json={"group": "beta"})
    assert res.status_code == 200, res.text
    u = next(x for x in sb._users if x.id == THIRD_USER_ID)
    assert u.app_metadata == {"provider": "email", "group": "beta"}
    assert next(a for a in client.get("/admin/accounts").json() if a["user_id"] == THIRD_USER_ID)["group"] == "beta"

    assert client.patch(f"/admin/accounts/{THIRD_USER_ID}", json={"group": None}).status_code == 200
    assert u.app_metadata == {"provider": "email"}


def test_set_account_group_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _sb())
    assert client.patch(f"/admin/accounts/{THIRD_USER_ID}", json={"group": "vip"}).status_code == 400
    assert client.patch(f"/admin/accounts/{GONE_OWNER_ID}", json={"group": "beta"}).status_code == 404
    assert client.patch("/admin/accounts/not-uuid", json={"group": "beta"}).status_code == 404
    non_admin = _client(monkeypatch, _sb(), admins=OTHER_USER_ID)
    assert non_admin.patch(f"/admin/accounts/{THIRD_USER_ID}", json={"group": "beta"}).status_code == 403


# ── X-Act-As-User 대행 ────────────────────────────────────────────────────

def test_act_as_lists_target_users_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """관리자가 헤더를 붙이면 기존 GET /devices 가 그 사용자 기기를 돌려준다."""
    client = _client(monkeypatch, _sb())
    res = client.get("/devices", headers={"X-Act-As-User": OTHER_USER_ID})
    assert res.status_code == 200, res.text
    assert [d["device_id"] for d in res.json()] == ["terra-aaaa"]
    # 헤더 없으면 본인(admin) 것 — 기기 없음
    assert client.get("/devices").json() == []


def test_act_as_non_admin_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _sb(), admins=OTHER_USER_ID)
    res = client.get("/devices", headers={"X-Act-As-User": OTHER_USER_ID})
    assert res.status_code == 403
    assert TEST_USER_ID in res.json()["detail"]


def test_act_as_bad_uuid_is_400_and_blank_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _sb())
    assert client.get("/devices", headers={"X-Act-As-User": "not-a-uuid"}).status_code == 400
    assert client.get("/devices", headers={"X-Act-As-User": "   "}).status_code == 200


def test_act_as_command_is_issued_by_target_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """대행으로 넣은 명령은 issued_by 가 그 사용자 — 앱 이력에서 본인 명령으로 보인다."""
    sb = _sb()
    client = _client(monkeypatch, sb)
    res = client.post("/devices/dev-terra-aaaa/commands", json={"action": "led_toggle"},
                      headers={"X-Act-As-User": OTHER_USER_ID})
    assert res.status_code == 201, res.text
    row = sb.one("commands", id=res.json()["id"])
    assert row is not None and row["issued_by"] == OTHER_USER_ID and row["device_id"] == "dev-terra-aaaa"
    # 대행 없이(admin 본인) 같은 기기는 남의 것 → 404
    assert client.post("/devices/dev-terra-aaaa/commands", json={"action": "led_toggle"}).status_code == 404
