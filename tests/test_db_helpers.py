"""db_helpers + 사용자 입력 id 단건 조회 자리들의 회귀 테스트.

실 PostgREST 는 `.single()` 0행이면 406 → 500, uuid 아닌 id 는 22P02 → 500.
MagicMock 은 이걸 재현 못 하므로 "DB 를 안 부른다" / "0행 리스트면 기존 응답 코드" 로 고정한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.db_helpers import first_row, parse_uuid, parse_uuid_or_404
from backend.routers import cameras, devices, webrtc
from tests.conftest import TEST_USER_ID

UUID = "abcdef12-3456-4890-abcd-ef1234567890"


# ---------- 헬퍼 ----------


@pytest.mark.parametrize("raw", [UUID, UUID.upper(), f"urn:uuid:{UUID}", "{" + UUID + "}"])
def test_parse_uuid_normalizes(raw: str) -> None:
    assert parse_uuid(raw) == UUID


@pytest.mark.parametrize("raw", [None, "", "cam-uuid", "p4cam-aabbccdd", UUID[:-1]])
def test_parse_uuid_rejects(raw: str | None) -> None:
    assert parse_uuid(raw) is None


def test_parse_uuid_or_404() -> None:
    assert parse_uuid_or_404(UUID, "x not found") == UUID
    with pytest.raises(HTTPException) as exc:
        parse_uuid_or_404("nope", "x not found")
    assert exc.value.status_code == 404
    assert exc.value.detail == "x not found"


def test_first_row() -> None:
    assert first_row([{"a": 1}, {"a": 2}]) == {"a": 1}
    assert first_row([]) is None
    assert first_row(None) is None


# ---------- 라우터: uuid 아닌 path id → 기존 not-found, DB 안 부름 ----------


_GET_PATHS = [
    ("get", "/cameras/{id}"),
    ("get", "/cameras/{id}/logs"),
    ("post", "/cameras/{id}/reboot"),
    ("get", "/devices/{id}"),
    ("get", "/enclosures/{id}"),
]


@pytest.mark.parametrize(("method", "path"), _GET_PATHS)
def test_bad_uuid_path_is_404_without_db(
    app_client: TestClient, fake_sb: MagicMock, method: str, path: str
) -> None:
    res = getattr(app_client, method)(path.format(id="not-a-uuid"))
    assert res.status_code == 404
    fake_sb.table.assert_not_called()


@pytest.mark.parametrize(("method", "path"), _GET_PATHS)
def test_missing_row_is_404(
    app_client: TestClient, fake_sb: MagicMock, method: str, path: str
) -> None:
    """0행 = 빈 리스트 → 404 (.single() 이었다면 실 DB 에서 406 → 500)."""
    fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value \
        .execute.return_value.data = []
    res = getattr(app_client, method)(path.format(id=UUID))
    assert res.status_code == 404


def test_webrtc_owned_camera_bad_uuid_404(fake_sb: MagicMock) -> None:
    with pytest.raises(HTTPException) as exc:
        webrtc._owned_camera("not-a-uuid", TEST_USER_ID)
    assert exc.value.status_code == 404
    fake_sb.table.assert_not_called()


def test_webrtc_owned_camera_missing_404(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    monkeypatch.setattr(webrtc, "get_supabase_client", lambda: fake_sb)
    fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value \
        .execute.return_value.data = []
    with pytest.raises(HTTPException) as exc:
        webrtc._owned_camera(UUID, TEST_USER_ID)
    assert exc.value.status_code == 404


# ---------- _verify_enclosure_owner: 기존대로 400 ----------


@pytest.mark.parametrize("module", [cameras, devices])
@pytest.mark.parametrize("data", [None, []])
def test_verify_enclosure_owner_bad_or_missing_is_400(
    fake_sb: MagicMock, module: object, data: list | None
) -> None:
    """data=None 은 uuid 아닌 입력(DB 안 부름), [] 는 0행."""
    enclosure_id = "enc-foreign" if data is None else UUID
    fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value \
        .execute.return_value.data = data or []
    with pytest.raises(HTTPException) as exc:
        module._verify_enclosure_owner(fake_sb, enclosure_id, TEST_USER_ID)
    assert exc.value.status_code == 400
    if data is None:
        fake_sb.table.assert_not_called()
