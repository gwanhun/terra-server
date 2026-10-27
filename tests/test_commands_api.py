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


# ── 토글·온도 보정 명령 + 상태 조회 (2026-10-27, 운영 콘솔 대행용) ─────────────

def _cmd_tables(fake_sb: MagicMock, inserted_id: str = "cmd-g") -> MagicMock:
    dev = _device_mock()
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": inserted_id}]
    tables = {"devices": dev, "commands": cmd}
    fake_sb.table.side_effect = lambda name: tables[name]
    return cmd


def test_generic_command_toggle_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _cmd_tables(fake_sb)
    res = app_client.post(f"/devices/{DEVICE_UUID}/commands", json={"action": "fan2_toggle"})
    assert res.status_code == 201, res.text
    assert res.json() == {"id": "cmd-g", "action": "fan2_toggle", "status": "pending"}
    payload = cmd.insert.call_args.args[0]
    assert payload["action"] == "fan2_toggle" and payload["payload"] is None
    assert payload["issued_by"] == TEST_USER_ID and payload["source"] == "manual"


def test_generic_command_temp_offset_validates_range(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _cmd_tables(fake_sb)
    ok = app_client.post(f"/devices/{DEVICE_UUID}/commands",
                         json={"action": "set_temp_offset", "payload": {"offset_c": -1.5}})
    assert ok.status_code == 201, ok.text
    assert cmd.insert.call_args.args[0]["payload"] == {"offset_c": -1.5}
    for bad in ({"offset_c": 10.5}, {"offset_c": "2"}, {}, None):
        res = app_client.post(f"/devices/{DEVICE_UUID}/commands", json={"action": "set_temp_offset", "payload": bad})
        assert res.status_code == 400, (bad, res.text)


def test_generic_command_rejects_unknown_action_and_stray_payload(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _cmd_tables(fake_sb)
    assert app_client.post(f"/devices/{DEVICE_UUID}/commands", json={"action": "token_rotate"}).status_code == 400
    assert app_client.post(f"/devices/{DEVICE_UUID}/commands", json={"action": "mist", "payload": {"duration_ms": 1000}}).status_code == 400
    assert app_client.post(f"/devices/{DEVICE_UUID}/commands", json={"action": "led_toggle", "payload": {"x": 1}}).status_code == 400
    cmd.insert.assert_not_called()


def test_generic_command_other_users_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock(owner=OTHER_USER_ID)
    fake_sb.table.side_effect = lambda name: {"devices": dev}[name]
    assert app_client.post(f"/devices/{DEVICE_UUID}/commands", json={"action": "led_toggle"}).status_code == 404


def test_get_command_status(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    dev = _device_mock()
    cmd = MagicMock()
    (cmd.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data) = [
        {"id": cmd_id, "action": "led_toggle", "status": "acked", "result": "ok",
         "issued_at": "2026-10-27T00:00:00+00:00", "acked_at": "2026-10-27T00:00:01+00:00"}
    ]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]
    res = app_client.get(f"/devices/{DEVICE_UUID}/commands/{cmd_id}")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "acked" and res.json()["result"] == "ok"
    # device_id 도 함께 걸러 남의 기기 명령 id 로 못 본다
    eq_calls = [c.args for c in cmd.select.return_value.eq.call_args_list] + \
               [c.args for c in cmd.select.return_value.eq.return_value.eq.call_args_list]
    assert ("device_id", DEVICE_UUID) in eq_calls


def test_get_command_status_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    dev = _device_mock()
    cmd = MagicMock()
    cmd.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]
    assert app_client.get(f"/devices/{DEVICE_UUID}/commands/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").status_code == 404
    assert app_client.get(f"/devices/{DEVICE_UUID}/commands/not-uuid").status_code == 404
