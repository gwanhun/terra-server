"""commands 라우터 (mist) 통합 테스트."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tests.conftest import TEST_USER_ID

DEVICE_UUID = "dev-1"


def _device_mock(owner: str = TEST_USER_ID) -> MagicMock:
    m = MagicMock()
    m.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"id": DEVICE_UUID, "owner_id": owner}
    ]
    return m


def test_mist_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-1"}]
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": 2000})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["action"] == "mist" and body["status"] == "pending"

    payload = cmd.insert.call_args.args[0]
    assert payload["action"] == "mist"
    assert payload["payload"] == {"duration_ms": 2000}
    assert payload["issued_by"] == TEST_USER_ID
    assert payload["status"] == "pending"


def test_mist_invalid_duration_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    cmd = MagicMock()
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": 1500})
    assert res.status_code == 400, res.text
    cmd.insert.assert_not_called()


def test_mist_foreign_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock(owner="other-user")
    cmd = MagicMock()
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": 2000})
    assert res.status_code == 404, res.text
    cmd.insert.assert_not_called()


def test_mist_missing_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = MagicMock()
    dev.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": MagicMock()}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": 1000})
    assert res.status_code == 404, res.text
