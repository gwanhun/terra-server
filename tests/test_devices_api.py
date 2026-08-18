"""devices 라우터 통합 테스트 (Supabase mock).

주로 사육장(enclosure) 배정 관련 신규 동작을 검증. 페어링/조회의 기본 흐름은
cameras 라우터와 동일 패턴이라 여기서는 enclosure_id 처리에 집중.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tests.conftest import TEST_USER_ID


def test_pair_device_with_enclosure_ok(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """본인 소유 사육장으로 페어링하면 enclosure_id 가 INSERT payload 에 담긴다."""
    enc_mock = MagicMock()
    enc_mock.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "owner_id": TEST_USER_ID
    }
    dev_mock = MagicMock()
    dev_mock.insert.return_value.execute.return_value.data = [
        {"id": "dev-1", "device_id": "terra-abcd"}
    ]

    tables = {"enclosures": enc_mock, "devices": dev_mock}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(
        "/devices/pair",
        json={"name": "거실 컨트롤러", "enclosure_id": "enc-1"},
    )
    assert res.status_code == 201, res.text

    payload = dev_mock.insert.call_args.args[0]
    assert payload["enclosure_id"] == "enc-1"
    assert payload["owner_id"] == TEST_USER_ID


def test_pair_device_with_foreign_enclosure_returns_400(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """남의 사육장 UUID 로 페어링하면 400 (device INSERT 도 안 됨)."""
    enc_mock = MagicMock()
    enc_mock.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "owner_id": "other-user"
    }
    dev_mock = MagicMock()

    tables = {"enclosures": enc_mock, "devices": dev_mock}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(
        "/devices/pair",
        json={"name": "몰래 컨트롤러", "enclosure_id": "enc-foreign"},
    )
    assert res.status_code == 400, res.text
    dev_mock.insert.assert_not_called()


def test_pair_device_without_enclosure_ok(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """enclosure_id 없이 페어링하면 단독 디바이스 (검증 쿼리 없이 INSERT)."""
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "dev-1", "device_id": "terra-solo"}
    ]

    res = app_client.post("/devices/pair", json={"name": "단독 컨트롤러"})
    assert res.status_code == 201, res.text

    payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert payload["enclosure_id"] is None


def test_pair_device_with_capabilities_stored(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """§2: 펌웨어가 보고한 capabilities 가 INSERT payload 에 담긴다."""
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "dev-1", "device_id": "terra-cap"}
    ]
    caps = {"board": "mosfet", "led_dimmable": True, "heater": True}

    res = app_client.post(
        "/devices/pair",
        json={"name": "밝기 되는 컨트롤러", "capabilities": caps},
    )
    assert res.status_code == 201, res.text
    payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert payload["capabilities"] == caps


def test_list_devices_exposes_capabilities(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """§2: 목록 응답에 capabilities 가 노출된다 (앱 밝기 슬라이더 판단용)."""
    caps = {"board": "relay", "led_dimmable": False}
    fake_sb.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value.data = [
        {
            "id": "dev-1",
            "device_id": "terra-abcd",
            "enclosure_id": None,
            "name": "거실",
            "species": None,
            "firmware_ver": None,
            "capabilities": caps,
            "created_at": "2026-05-27T00:00:00Z",
            "last_seen_at": None,
            "is_online": False,
        }
    ]

    res = app_client.get("/devices")
    assert res.status_code == 200, res.text
    assert res.json()[0]["capabilities"] == caps


def test_update_device_assign_enclosure_ok(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """PATCH 로 사육장 배정 시 소유권 검증 후 업데이트."""
    enc_mock = MagicMock()
    enc_mock.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "owner_id": TEST_USER_ID
    }
    dev_mock = MagicMock()
    dev_mock.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {
            "id": "dev-1",
            "device_id": "terra-abcd",
            "enclosure_id": "enc-1",
            "name": "거실 컨트롤러",
            "species": None,
            "firmware_ver": None,
            "created_at": "2026-05-27T00:00:00Z",
            "last_seen_at": None,
            "is_online": False,
        }
    ]

    tables = {"enclosures": enc_mock, "devices": dev_mock}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.patch("/devices/dev-1", json={"enclosure_id": "enc-1"})
    assert res.status_code == 200, res.text
    assert res.json()["enclosure_id"] == "enc-1"


def test_update_device_foreign_enclosure_returns_400(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """남의 사육장으로 배정 시도하면 400 (update 안 됨)."""
    enc_mock = MagicMock()
    enc_mock.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "owner_id": "other-user"
    }
    dev_mock = MagicMock()

    tables = {"enclosures": enc_mock, "devices": dev_mock}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.patch("/devices/dev-1", json={"enclosure_id": "enc-foreign"})
    assert res.status_code == 400, res.text
    dev_mock.update.assert_not_called()


def test_update_device_unassign_enclosure_ok(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """enclosure_id=null 로 사육장에서 분리 (None 은 소유권 검증 스킵)."""
    fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {
            "id": "dev-1",
            "device_id": "terra-abcd",
            "enclosure_id": None,
            "name": "거실 컨트롤러",
            "species": None,
            "firmware_ver": None,
            "created_at": "2026-05-27T00:00:00Z",
            "last_seen_at": None,
            "is_online": False,
        }
    ]

    res = app_client.patch("/devices/dev-1", json={"enclosure_id": None})
    assert res.status_code == 200, res.text
    assert res.json()["enclosure_id"] is None
