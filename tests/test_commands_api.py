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


# ---------- 분사 시간 5/7/10초 (앱 0.131.0) — 기기 상한(capabilities.mist_max_ms) ----------

def _capable_device_mock(mist_max_ms: int | None) -> MagicMock:
    caps = {"board": "mosfet", "led_dimmable": True}
    if mist_max_ms is not None:
        caps["mist_max_ms"] = mist_max_ms
    m = MagicMock()
    m.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"id": DEVICE_UUID, "owner_id": TEST_USER_ID, "capabilities": caps}
    ]
    return m


def test_mist_10s_accepted_when_device_reports_max(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _capable_device_mock(10000)
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-1"}]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": 10000})
    assert res.status_code == 201, res.text
    assert cmd.insert.call_args.args[0]["payload"] == {"duration_ms": 10000}


def test_mist_10s_rejected_on_old_firmware(app_client: TestClient, fake_sb: MagicMock) -> None:
    """mist_max_ms 미보고(구 펌웨어) → 5000 초과는 400. 펌웨어가 조용히 5초로 자르는 것을 막는다."""
    dev = _capable_device_mock(None)
    cmd = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": 10000})
    assert res.status_code == 400, res.text
    assert "mist_max_ms" in res.json()["detail"]
    cmd.insert.assert_not_called()


def test_mist_legacy_values_always_accepted(app_client: TestClient, fake_sb: MagicMock) -> None:
    """저장된 옛 예약(1/2/3초)은 구 펌웨어에서도 계속 통과해야 한다 (앱 요청 1)."""
    dev = _capable_device_mock(None)
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-1"}]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]
    for ms in (1000, 2000, 3000, 5000):
        assert app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": ms}).status_code == 201, ms
