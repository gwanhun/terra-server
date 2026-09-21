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


def _dev_mock_with_hw_lookup(existing: list[dict]) -> MagicMock:
    """devices 테이블 목: hw_id 조회 체인 + insert/update 준비."""
    dev = MagicMock()
    (dev.select.return_value.eq.return_value.eq.return_value
     .is_.return_value.limit.return_value.execute.return_value.data) = existing
    dev.insert.return_value.execute.return_value.data = [
        {"id": "dev-1", "device_id": "terra-abcd"}
    ]
    dev.update.return_value.eq.return_value.execute.return_value.data = [
        {"id": "dev-1", "device_id": "terra-abcd"}
    ]
    return dev


def test_pair_device_new_hw_id_inserts_and_stores_it(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """처음 보는 hw_id → 기존대로 새 행 생성. hw_id 가 INSERT payload 에 들어간다."""
    dev = _dev_mock_with_hw_lookup([])
    fake_sb.table.side_effect = lambda name: {"devices": dev}[name]

    res = app_client.post(
        "/devices/pair",
        json={"name": "거실 컨트롤러", "hw_id": "A0B7651C2908"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["reused"] is False
    assert dev.insert.call_args.args[0]["hw_id"] == "A0B7651C2908"


def test_pair_device_same_hw_id_reuses_row_instead_of_inserting(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """같은 보드 재페어링(WiFi 변경) → 새 행 없이 기존 행 갱신."""
    dev = _dev_mock_with_hw_lookup([{"id": "dev-1", "device_id": "terra-abcd"}])
    fake_sb.table.side_effect = lambda name: {"devices": dev}[name]

    res = app_client.post(
        "/devices/pair",
        json={"name": "거실 컨트롤러", "hw_id": "A0B7651C2908"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["reused"] is True
    assert body["device_id"] == "terra-abcd"   # device_id 유지
    assert len(body["mqtt_token"]) > 20        # 토큰은 새로 발급

    dev.insert.assert_not_called()
    patch = dev.update.call_args.args[0]
    assert patch["token_hash"].startswith("$2")
    assert patch["token_hash"] != body["mqtt_token"]


def test_pair_device_reuse_keeps_enclosure_when_not_given(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """enclosure_id 미지정 재페어링이 기존 사육장 연결을 끊지 않는다."""
    dev = _dev_mock_with_hw_lookup([{"id": "dev-1", "device_id": "terra-abcd"}])
    fake_sb.table.side_effect = lambda name: {"devices": dev}[name]

    res = app_client.post(
        "/devices/pair",
        json={"name": "거실 컨트롤러", "hw_id": "A0B7651C2908"},
    )
    assert res.status_code == 201, res.text
    assert "enclosure_id" not in dev.update.call_args.args[0]


def test_pair_device_without_hw_id_never_reuses(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """구 펌웨어(hw_id 없음) → 조회 없이 기존 동작(항상 새 행) 유지."""
    dev = _dev_mock_with_hw_lookup([{"id": "other", "device_id": "terra-zzzz"}])
    fake_sb.table.side_effect = lambda name: {"devices": dev}[name]

    res = app_client.post("/devices/pair", json={"name": "거실 컨트롤러"})
    assert res.status_code == 201, res.text
    assert res.json()["reused"] is False
    dev.insert.assert_called_once()


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


def test_pair_device_without_capabilities_gets_default(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """웹 등록 패널처럼 capabilities 를 안 보내면 기본 보드 플래그가 저장된다
    (null 이면 앱이 밝기 슬라이더를 숨김 — 2026-09-18 베타기기 2908 사례)."""
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "dev-1", "device_id": "terra-web"}
    ]
    res = app_client.post("/devices/pair", json={"name": "베타기기"})
    assert res.status_code == 201, res.text
    payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert payload["capabilities"] == {"board": "mosfet", "led_dimmable": True}


def test_list_devices_exposes_capabilities(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """§2: 목록 응답에 capabilities 가 노출된다 (앱 밝기 슬라이더 판단용)."""
    caps = {"board": "relay", "led_dimmable": False}
    fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.order.return_value.execute.return_value.data = [
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
    dev_mock.update.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
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
    fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
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
