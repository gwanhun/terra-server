"""LCD 라우터 통합 테스트."""

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


def test_lcd_text_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-1"}]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd", json={"text": "밥 6시"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["action"] == "lcd_bitmap" and body["status"] == "pending"

    payload = cmd.insert.call_args.args[0]
    assert payload["action"] == "lcd_bitmap"
    assert payload["payload"]["w"] == 128 and payload["payload"]["h"] == 24
    assert payload["payload"]["enc"] == "raw"
    assert len(payload["payload"]["data"]) > 100      # base64 비트맵
    assert payload["issued_by"] == TEST_USER_ID


def test_lcd_empty_text_clears(app_client: TestClient, fake_sb: MagicMock) -> None:
    """빈 텍스트 → lcd_clear 명령."""
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-2"}]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd", json={"text": "   "})
    assert res.status_code == 201, res.text
    assert res.json()["action"] == "lcd_clear"
    assert cmd.insert.call_args.args[0]["payload"] is None


def test_lcd_clear_endpoint(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-3"}]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd/clear")
    assert res.status_code == 201, res.text
    assert res.json()["action"] == "lcd_clear"


def test_lcd_foreign_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock(owner="other-user")
    cmd = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd", json={"text": "hi"})
    assert res.status_code == 404, res.text
    cmd.insert.assert_not_called()


def test_lcd_text_too_long_422(app_client: TestClient, fake_sb: MagicMock) -> None:
    """max_length 초과 → Pydantic 422."""
    dev = _device_mock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": MagicMock()}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd", json={"text": "가" * 100})
    assert res.status_code == 422, res.text
