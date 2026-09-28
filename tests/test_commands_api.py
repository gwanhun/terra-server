"""commands 라우터 (mist) 통합 테스트."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tests.conftest import OTHER_USER_ID, TEST_USER_ID

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


def test_reboot_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    """원격 재부팅(2026-09-28): commands 에 action=reboot, ttl 60 큐잉. payload 없음."""
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-rb"}]
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/reboot")
    assert res.status_code == 201, res.text
    assert res.json() == {"id": "cmd-rb", "action": "reboot", "status": "pending"}

    payload = cmd.insert.call_args.args[0]
    assert payload["action"] == "reboot"
    assert payload["payload"] is None
    assert payload["ttl_sec"] == 60
    assert payload["issued_by"] == TEST_USER_ID


def test_reboot_foreign_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock(owner=OTHER_USER_ID)
    cmd = MagicMock()
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]

    res = app_client.post(f"/devices/{DEVICE_UUID}/reboot")
    assert res.status_code == 404, res.text
    cmd.insert.assert_not_called()


def test_mist_invalid_duration_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    cmd = MagicMock()
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]

    for bad in (500, 20001, 0, -1000):          # 범위 밖 (1000~20000)
        res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": bad})
        assert res.status_code == 400, (bad, res.text)
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


# ---------- 분사 시간 5/10초 (2026-09-23) — 상한 초과는 발행 시 분할하므로 REST 는 형식만 본다 ----------

def test_mist_5s_10s_and_legacy_values_accepted(app_client: TestClient, fake_sb: MagicMock) -> None:
    """1000~20000 범위의 임의 정수 전부 201 (2026-09-28 화이트리스트 폐지). 기기 상한은 dispatcher 가 분할로 채운다."""
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": "cmd-1"}]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]
    for ms in (1000, 1500, 3000, 4200, 7000, 13000, 20000):
        res = app_client.post(f"/devices/{DEVICE_UUID}/mist", json={"duration_ms": ms})
        assert res.status_code == 201, (ms, res.text)
    assert cmd.insert.call_args.args[0]["payload"] == {"duration_ms": 20000}   # 요청값 그대로 저장
