"""schedules 라우터 통합 테스트 (생성/검증/삭제)."""

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


def _schedule_row(**over) -> dict:
    row = {
        "id": "sch-1",
        "device_id": DEVICE_UUID,
        "action": "mist",
        "payload": {"duration_ms": 2000},
        "kind": "daily",
        "time_of_day": "08:00:00",
        "days_of_week": None,
        "enabled": True,
        "next_run_at": "2026-08-10T23:00:00+00:00",
        "last_run_at": None,
        "created_at": "2026-08-10T00:00:00+00:00",
    }
    row.update(over)
    return row


def test_create_daily_mist_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    sch.insert.return_value.execute.return_value.data = [_schedule_row()]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={
            "action": "mist",
            "payload": {"duration_ms": 2000},
            "kind": "daily",
            "time_of_day": "08:00",
        },
    )
    assert res.status_code == 201, res.text

    payload = sch.insert.call_args.args[0]
    assert payload["owner_id"] == TEST_USER_ID
    assert payload["action"] == "mist"
    assert payload["days_of_week"] is None
    # next_run_at 을 서버가 계산해 넣었는지
    assert payload["next_run_at"].endswith("+00:00")


def test_create_weekly_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    sch.insert.return_value.execute.return_value.data = [
        _schedule_row(kind="weekly", days_of_week=[1, 3, 5])
    ]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={
            "action": "fan_toggle",
            "kind": "weekly",
            "time_of_day": "20:00",
            "days_of_week": [1, 3, 5],
        },
    )
    assert res.status_code == 201, res.text
    payload = sch.insert.call_args.args[0]
    assert payload["days_of_week"] == [1, 3, 5]


def test_create_weekly_without_days_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "fan_toggle", "kind": "weekly", "time_of_day": "20:00"},
    )
    assert res.status_code == 400, res.text
    sch.insert.assert_not_called()


def test_create_mist_bad_duration_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={
            "action": "mist",
            "payload": {"duration_ms": 9999},
            "kind": "daily",
            "time_of_day": "08:00",
        },
    )
    assert res.status_code == 400, res.text
    sch.insert.assert_not_called()


def test_create_disallowed_action_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "self_destruct", "kind": "daily", "time_of_day": "08:00"},
    )
    assert res.status_code == 400, res.text


def test_create_foreign_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock(owner="other-user")
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "mist", "payload": {"duration_ms": 1000},
              "kind": "daily", "time_of_day": "08:00"},
    )
    assert res.status_code == 404, res.text


def test_bad_time_format_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "led_on", "kind": "daily", "time_of_day": "25:99"},
    )
    assert res.status_code == 400, res.text


def test_delete_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    sch = MagicMock()
    sch.delete.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "sch-1"}
    ]
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.delete("/schedules/sch-1")
    assert res.status_code == 204, res.text


def test_delete_missing_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    sch = MagicMock()
    sch.delete.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.delete("/schedules/nope")
    assert res.status_code == 404, res.text
