"""device_settings 라우터 테스트 (앱 §5) — 목표 환경 조회/수정."""

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


def _settings_mock(row: dict | None) -> MagicMock:
    m = MagicMock()
    m.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [row] if row is not None else []
    )
    m.upsert.return_value.execute.return_value.data = [row] if row is not None else [{}]
    return m


def test_get_settings_empty_returns_nulls(app_client: TestClient, fake_sb: MagicMock) -> None:
    """설정 행이 없으면 값이 모두 null 인 빈 설정 (404 아님)."""
    dev = _device_mock()
    ds = _settings_mock(None)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.get(f"/devices/{DEVICE_UUID}/settings")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["device_id"] == DEVICE_UUID
    assert body["target_temp_c"] is None


def test_get_settings_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    row = {"device_id": DEVICE_UUID, "target_temp_c": 28.0, "target_humidity_pct": 60.0}
    ds = _settings_mock(row)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.get(f"/devices/{DEVICE_UUID}/settings")
    assert res.status_code == 200, res.text
    assert res.json()["target_temp_c"] == 28.0


def test_get_settings_foreign_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock(owner="other-user")
    ds = _settings_mock(None)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.get(f"/devices/{DEVICE_UUID}/settings")
    assert res.status_code == 404, res.text


def test_patch_settings_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    row = {"device_id": DEVICE_UUID, "target_temp_c": 30.0, "target_humidity_pct": 55.0}
    ds = _settings_mock(row)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.patch(
        f"/devices/{DEVICE_UUID}/settings",
        json={"target_temp_c": 30, "target_humidity_pct": 55},
    )
    assert res.status_code == 200, res.text
    assert res.json()["target_temp_c"] == 30.0
    # upsert payload 에 device_id 가 붙었는지
    assert ds.upsert.call_args.args[0]["device_id"] == DEVICE_UUID


def test_patch_settings_out_of_range_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    ds = _settings_mock(None)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.patch(
        f"/devices/{DEVICE_UUID}/settings", json={"target_temp_c": 200}
    )
    assert res.status_code == 400, res.text
    ds.upsert.assert_not_called()


def test_patch_settings_humidity_out_of_range_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    ds = _settings_mock(None)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.patch(
        f"/devices/{DEVICE_UUID}/settings", json={"target_humidity_pct": 150}
    )
    assert res.status_code == 400, res.text
    ds.upsert.assert_not_called()


def test_patch_settings_min_gt_max_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    ds = _settings_mock(None)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.patch(
        f"/devices/{DEVICE_UUID}/settings",
        json={"target_temp_min": 35, "target_temp_max": 25},
    )
    assert res.status_code == 400, res.text
    ds.upsert.assert_not_called()


def test_patch_settings_empty_body_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    ds = _settings_mock(None)
    fake_sb.table.side_effect = lambda name: {"devices": dev, "device_settings": ds}[name]

    res = app_client.patch(f"/devices/{DEVICE_UUID}/settings", json={})
    assert res.status_code == 400, res.text
