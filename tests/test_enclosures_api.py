"""enclosures 라우터 통합 테스트 (Supabase mock)."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tests.conftest import TEST_USER_ID


def test_create_enclosure(app_client: TestClient, fake_sb: MagicMock) -> None:
    row = {
        "id": "enc-1",
        "name": "거실",
        "species": "bearded_dragon",
        "note": None,
        "created_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
    }
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [row]

    res = app_client.post(
        "/enclosures",
        json={"name": "거실", "species": "bearded_dragon"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["id"] == "enc-1"

    # owner_id 가 INSERT payload 에 박혀있는지 검증
    insert_call = fake_sb.table.return_value.insert.call_args
    assert insert_call.args[0]["owner_id"] == TEST_USER_ID


def test_list_enclosures(app_client: TestClient, fake_sb: MagicMock) -> None:
    rows = [
        {
            "id": "enc-1",
            "name": "거실",
            "species": None,
            "note": None,
            "created_at": "2026-05-27T00:00:00Z",
            "updated_at": "2026-05-27T00:00:00Z",
        }
    ]
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.order.return_value
    chain.execute.return_value.data = rows

    res = app_client.get("/enclosures")
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["name"] == "거실"


def test_list_enclosures_includes_camera_count(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """목록 응답에 사육장별 카메라 대수가 집계돼야 한다."""
    enc_rows = [
        {
            "id": "enc-1",
            "name": "거실",
            "species": None,
            "note": None,
            "created_at": "2026-05-27T00:00:00Z",
            "updated_at": "2026-05-27T00:00:00Z",
        },
        {
            "id": "enc-2",
            "name": "방",
            "species": None,
            "note": None,
            "created_at": "2026-05-27T00:00:00Z",
            "updated_at": "2026-05-27T00:00:00Z",
        },
    ]
    cam_rows = [
        {"enclosure_id": "enc-1"},
        {"enclosure_id": "enc-1"},
        {"enclosure_id": None},  # 단독 카메라는 카운트 안 됨
    ]

    dev_rows = [{"enclosure_id": "enc-2"}]

    enc_mock = MagicMock()
    enc_mock.select.return_value.eq.return_value.order.return_value.execute.return_value.data = enc_rows
    cam_mock = MagicMock()
    cam_mock.select.return_value.eq.return_value.execute.return_value.data = cam_rows
    dev_mock = MagicMock()
    dev_mock.select.return_value.eq.return_value.execute.return_value.data = dev_rows

    tables = {"enclosures": enc_mock, "cameras": cam_mock, "devices": dev_mock}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.get("/enclosures")
    assert res.status_code == 200, res.text
    by_id = {e["id"]: e for e in res.json()}
    assert by_id["enc-1"]["camera_count"] == 2
    assert by_id["enc-1"]["device_count"] == 0
    assert by_id["enc-2"]["camera_count"] == 0
    assert by_id["enc-2"]["device_count"] == 1


def test_get_enclosure_includes_cameras(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """단건 조회 시 소속 카메라 목록이 nested 로 포함돼야 한다."""
    enc_row = {
        "id": "enc-1",
        "owner_id": TEST_USER_ID,
        "name": "거실",
        "species": None,
        "note": None,
        "created_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
    }
    cam_rows = [
        {
            "id": "cam-1",
            "camera_id": "p4cam-aaaa",
            "name": "거실 캠",
            "model": "esp32-p4",
            "is_online": True,
            "stream_mode": None,
            "last_seen_at": "2026-05-27T00:00:00Z",
        }
    ]
    dev_rows = [
        {
            "id": "dev-1",
            "device_id": "terra-bbbb",
            "name": "거실 컨트롤러",
            "species": "bearded_dragon",
            "is_online": False,
            "last_seen_at": None,
        }
    ]

    enc_mock = MagicMock()
    enc_mock.select.return_value.eq.return_value.single.return_value.execute.return_value.data = enc_row
    cam_mock = MagicMock()
    cam_mock.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value.data = cam_rows
    dev_mock = MagicMock()
    dev_mock.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value.data = dev_rows

    tables = {"enclosures": enc_mock, "cameras": cam_mock, "devices": dev_mock}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.get("/enclosures/enc-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["camera_count"] == 1
    assert body["device_count"] == 1
    assert len(body["cameras"]) == 1
    assert body["cameras"][0]["camera_id"] == "p4cam-aaaa"
    assert body["cameras"][0]["is_online"] is True
    assert len(body["devices"]) == 1
    assert body["devices"][0]["device_id"] == "terra-bbbb"


def test_get_enclosure_not_owner_returns_404(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = {
        "id": "enc-1",
        "owner_id": "other-user",  # 다른 유저 소유
        "name": "x",
        "species": None,
        "note": None,
        "created_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
    }

    res = app_client.get("/enclosures/enc-1")
    assert res.status_code == 404


def test_update_enclosure_no_fields_returns_400(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    res = app_client.patch("/enclosures/enc-1", json={})
    assert res.status_code == 400


def test_update_enclosure_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    updated = {
        "id": "enc-1",
        "name": "방",
        "species": None,
        "note": None,
        "created_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
    }
    chain = fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value
    chain.execute.return_value.data = [updated]

    res = app_client.patch("/enclosures/enc-1", json={"name": "방"})
    assert res.status_code == 200
    assert res.json()["name"] == "방"


def test_delete_enclosure_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.delete.return_value.eq.return_value.eq.return_value
    chain.execute.return_value.data = [{"id": "enc-1"}]

    res = app_client.delete("/enclosures/enc-1")
    assert res.status_code == 204


def test_delete_enclosure_not_found(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    chain = fake_sb.table.return_value.delete.return_value.eq.return_value.eq.return_value
    chain.execute.return_value.data = []

    res = app_client.delete("/enclosures/enc-missing")
    assert res.status_code == 404
