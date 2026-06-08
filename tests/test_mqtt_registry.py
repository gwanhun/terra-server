"""registry 단위 테스트 — subprocess 는 mock."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.mqtt import registry


@pytest.fixture(autouse=True)
def _enable_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본은 disabled 라 호출 무시되므로 강제 활성."""
    monkeypatch.setenv("MOSQUITTO_REGISTRY_ENABLED", "true")
    monkeypatch.setenv("MOSQUITTO_HELPER_PATH", "/tmp/fake-helper.sh")
    monkeypatch.setenv("MQTT_BRIDGE_USERNAME", "terra-bridge")


# ---------- _enabled ----------


def test_disabled_skips_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOSQUITTO_REGISTRY_ENABLED", "false")
    with patch.object(registry.subprocess, "run") as run:
        ok = registry.register_device("terra-x", "tokenY")
    assert ok is True
    run.assert_not_called()


# ---------- register_device ----------


def test_register_device_calls_helper(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    monkeypatch.setattr(registry, "get_supabase_client", lambda: fake_sb)
    fake_sb.table.return_value.select.return_value.execute.return_value.data = []

    with patch.object(registry.subprocess, "run") as run, \
         patch.object(registry.shutil, "which", return_value="/usr/bin/sudo"):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok = registry.register_device("terra-aabbccdd", "plain-tok")

    assert ok is True
    # 첫 호출: register, 두 번째: regen-acl, 세 번째: reload
    assert run.call_count == 3
    first = run.call_args_list[0].args[0]
    assert first[-3:] == ["register", "terra-aabbccdd", "plain-tok"]


def test_register_helper_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    with patch.object(registry.subprocess, "run") as run, \
         patch.object(registry.shutil, "which", return_value="/usr/bin/sudo"):
        run.return_value = MagicMock(returncode=2, stdout="", stderr="oops")
        ok = registry.register_device("terra-x", "tok")
    assert ok is False
    # regen-acl 까지 안 감 (register 실패하면 early return)
    assert run.call_count == 1


def test_register_no_sudo_returns_false() -> None:
    with patch.object(registry.shutil, "which", return_value=None):
        ok = registry.register_device("terra-x", "tok")
    assert ok is False


def test_register_timeout_returns_false() -> None:
    with patch.object(registry.subprocess, "run") as run, \
         patch.object(registry.shutil, "which", return_value="/usr/bin/sudo"):
        run.side_effect = registry.subprocess.TimeoutExpired(cmd="x", timeout=5)
        ok = registry.register_device("terra-x", "tok")
    assert ok is False


# ---------- unregister_device ----------


def test_unregister_device_calls_helper(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    monkeypatch.setattr(registry, "get_supabase_client", lambda: fake_sb)
    fake_sb.table.return_value.select.return_value.execute.return_value.data = []

    with patch.object(registry.subprocess, "run") as run, \
         patch.object(registry.shutil, "which", return_value="/usr/bin/sudo"):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok = registry.unregister_device("terra-aabbccdd")

    assert ok is True
    # unregister + regen-acl + reload
    assert run.call_count == 3
    first = run.call_args_list[0].args[0]
    assert first[-2:] == ["unregister", "terra-aabbccdd"]


# ---------- regenerate_acl ----------


def test_regenerate_acl_builds_content_from_db(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    monkeypatch.setattr(registry, "get_supabase_client", lambda: fake_sb)

    devs = [{"device_id": "terra-aa"}, {"device_id": "terra-bb"}]
    cams = [{"camera_id": "p4cam-cc"}]

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "devices":
            t.select.return_value.execute.return_value.data = devs
        elif name == "cameras":
            t.select.return_value.execute.return_value.data = cams
        return t

    fake_sb.table.side_effect = _table

    captured_stdin: dict[str, str | None] = {"value": None}

    def _fake_run(cmd, **kwargs):
        # regen-acl 호출의 input 만 캡처 (reload 가 None 으로 덮어쓰는 거 방지)
        if "regen-acl" in cmd:
            captured_stdin["value"] = kwargs.get("input")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(registry.subprocess, "run", side_effect=_fake_run), \
         patch.object(registry.shutil, "which", return_value="/usr/bin/sudo"):
        ok = registry.regenerate_acl()

    assert ok is True
    content = captured_stdin["value"] or ""
    # bridge entry
    assert "user terra-bridge" in content
    assert "topic readwrite esp32/#" in content
    # devices
    assert "user terra-aa" in content
    assert "topic write esp32/terra-aa/telemetry" in content
    assert "topic read  esp32/terra-aa/command" in content
    assert "user terra-bb" in content
    # camera
    assert "user p4cam-cc" in content
    assert "topic write esp32/p4cam-cc/motion_event" in content


def test_regenerate_acl_skips_bridge_if_env_missing(
    monkeypatch: pytest.MonkeyPatch, fake_sb: MagicMock
) -> None:
    monkeypatch.delenv("MQTT_BRIDGE_USERNAME", raising=False)
    monkeypatch.setattr(registry, "get_supabase_client", lambda: fake_sb)
    fake_sb.table.return_value.select.return_value.execute.return_value.data = []

    captured: dict[str, str | None] = {"value": None}

    def _fake_run(cmd, **kwargs):
        captured["value"] = kwargs.get("input")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(registry.subprocess, "run", side_effect=_fake_run), \
         patch.object(registry.shutil, "which", return_value="/usr/bin/sudo"):
        registry.regenerate_acl()

    content = captured["value"] or ""
    assert "user terra-bridge" not in content
