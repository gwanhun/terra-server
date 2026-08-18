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
            "action": "fan_on",
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
        json={"action": "fan_on", "kind": "weekly", "time_of_day": "20:00"},
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


def test_create_onoff_actions_allowed(app_client: TestClient, fake_sb: MagicMock) -> None:
    """요청 1: heater_on/off, fan_on/off, relay_on/off 가 예약 허용 액션이어야."""
    for action in ("heater_on", "heater_off", "fan_on", "fan_off", "relay_on", "relay_off"):
        dev = _device_mock()
        sch = MagicMock()
        sch.insert.return_value.execute.return_value.data = [_schedule_row(action=action)]
        fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

        res = app_client.post(
            f"/devices/{DEVICE_UUID}/schedules",
            json={"action": action, "kind": "daily", "time_of_day": "20:00"},
        )
        assert res.status_code == 201, f"{action}: {res.text}"


def test_create_disallowed_action_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "self_destruct", "kind": "daily", "time_of_day": "08:00"},
    )
    assert res.status_code == 400, res.text


def test_create_toggle_rejected_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§6: *_toggle 은 예약 불가 (무인 실행 시 상태 어긋남 → 과열 위험)."""
    for action in ("relay_toggle", "fan_toggle", "heater_toggle", "led_toggle"):
        dev = _device_mock()
        sch = MagicMock()
        fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

        res = app_client.post(
            f"/devices/{DEVICE_UUID}/schedules",
            json={"action": action, "kind": "daily", "time_of_day": "08:00"},
        )
        assert res.status_code == 400, f"{action}: {res.text}"
        sch.insert.assert_not_called()


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


def test_create_with_guard_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    """요청 2: 유효한 skip 가드가 붙은 예약 생성."""
    dev = _device_mock()
    sch = MagicMock()
    guard = {"type": "skip_when_humidity_above", "value": 70, "enabled": True}
    sch.insert.return_value.execute.return_value.data = [_schedule_row(guard=guard)]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "mist", "payload": {"duration_ms": 2000},
              "kind": "daily", "time_of_day": "08:00", "guard": guard},
    )
    assert res.status_code == 201, res.text
    assert sch.insert.call_args.args[0]["guard"] == guard


def test_create_bad_guard_type_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "fan_on", "kind": "daily", "time_of_day": "08:00",
              "guard": {"type": "nuke_when_bored", "value": 1}},
    )
    assert res.status_code == 400, res.text
    sch.insert.assert_not_called()


def test_create_bad_guard_value_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "fan_on", "kind": "daily", "time_of_day": "08:00",
              "guard": {"type": "skip_when_temp_above", "value": "hot"}},
    )
    assert res.status_code == 400, res.text


def test_create_off_with_guard_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§4-3: off 계열 action 에 guard 를 걸면 400 (off 스킵 → 기기 켜진 채 위험)."""
    dev = _device_mock()
    sch = MagicMock()
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "heater_off", "kind": "daily", "time_of_day": "08:00",
              "guard": {"type": "skip_when_temp_above", "value": 35}},
    )
    assert res.status_code == 400, res.text
    sch.insert.assert_not_called()


def test_create_on_with_guard_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§4-3 반례: on 계열 action + guard 는 정상 허용."""
    dev = _device_mock()
    sch = MagicMock()
    guard = {"type": "skip_when_temp_above", "value": 35}
    sch.insert.return_value.execute.return_value.data = [
        _schedule_row(action="heater_on", guard=guard)
    ]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "heater_on", "kind": "daily", "time_of_day": "08:00", "guard": guard},
    )
    assert res.status_code == 201, res.text


def test_patch_add_guard_to_off_400(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§4-3: PATCH 로 off 예약에 guard 를 붙여도 400 (웹 콘솔 등 우회 차단)."""
    sch = MagicMock()
    sch.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        _schedule_row(action="fan_off", owner_id=TEST_USER_ID, guard=None)
    ]
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.patch(
        "/schedules/sch-1",
        json={"guard": {"type": "skip_when_humidity_below", "value": 40}},
    )
    assert res.status_code == 400, res.text
    sch.update.assert_not_called()


def test_patch_clear_guard_null_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§4-2: PATCH {"guard": null} 은 가드를 삭제(컬럼 NULL)한다 — 400 아님."""
    sch = MagicMock()
    sch.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        _schedule_row(action="mist", owner_id=TEST_USER_ID,
                      guard={"type": "skip_when_temp_above", "value": 35})
    ]
    sch.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        _schedule_row(action="mist", guard=None)
    ]
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.patch("/schedules/sch-1", json={"guard": None})
    assert res.status_code == 200, res.text
    assert sch.update.call_args.args[0]["guard"] is None


def test_create_with_pair_id_stored(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§3(B): 구간 예약 묶음 pair_id 가 INSERT payload 에 담긴다."""
    dev = _device_mock()
    sch = MagicMock()
    sch.insert.return_value.execute.return_value.data = [
        _schedule_row(action="heater_on", pair_id="pair-1")
    ]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "schedules": sch}[name]

    res = app_client.post(
        f"/devices/{DEVICE_UUID}/schedules",
        json={"action": "heater_on", "kind": "daily", "time_of_day": "20:00",
              "pair_id": "pair-1"},
    )
    assert res.status_code == 201, res.text
    assert sch.insert.call_args.args[0]["pair_id"] == "pair-1"


def test_delete_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    """단건(pair_id 없음) 삭제 — 자신만 삭제."""
    sch = MagicMock()
    sch.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        _schedule_row(owner_id=TEST_USER_ID, pair_id=None)
    ]
    sch.delete.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "sch-1"}
    ]
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.delete("/schedules/sch-1")
    assert res.status_code == 204, res.text
    # pair_id 없으면 id 로 삭제
    assert sch.delete.return_value.eq.return_value.eq.call_args.args == ("id", "sch-1")


def test_delete_pair_cascade(app_client: TestClient, fake_sb: MagicMock) -> None:
    """§3(B): pair_id 있는 예약 삭제 시 짝(on/off)까지 pair_id 로 일괄 삭제."""
    sch = MagicMock()
    sch.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        _schedule_row(owner_id=TEST_USER_ID, pair_id="pair-1")
    ]
    sch.delete.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "sch-on"}, {"id": "sch-off"}
    ]
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.delete("/schedules/sch-on")
    assert res.status_code == 204, res.text
    # 짝 삭제는 pair_id 기준
    assert sch.delete.return_value.eq.return_value.eq.call_args.args == ("pair_id", "pair-1")


def test_delete_missing_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    sch = MagicMock()
    sch.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    fake_sb.table.side_effect = lambda name: {"schedules": sch}[name]

    res = app_client.delete("/schedules/nope")
    assert res.status_code == 404, res.text
