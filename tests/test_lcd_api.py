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
    # 원문은 ACK 때 devices.lcd_text 로 확정하려고 같이 남긴다 (기기엔 안 나감 — dispatcher 가 뺀다)
    assert payload["payload"]["lcd_text"] == "밥 6시"


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


# ---------------------------------------------------------------------------
# Stage N — 화면보호기 / 전원 / 소등 시간대
# ---------------------------------------------------------------------------

import hashlib
from io import BytesIO

import pytest
from PIL import Image

from backend.routers import lcd as lcd_router


def _saver_device_mock(owner: str = TEST_USER_ID, caps: dict | None = None) -> MagicMock:
    """capabilities.lcd_saver 를 보고한 기기(기본). caps 를 주면 그대로."""
    m = MagicMock()
    row = {"id": DEVICE_UUID, "owner_id": owner,
           "capabilities": {"board": "mosfet", "lcd_saver": True} if caps is None else caps}
    m.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [row]
    return m


def _wire(fake_sb: MagicMock, dev: MagicMock, cmd_id: str = "cmd-n",
          inflight_shas: list[str] | None = None) -> MagicMock:
    """commands 스텁. inflight_shas = 아직 pending/sent 인 lcd_image 명령의 sha256 목록(고아 정리가 조회)."""
    cmd = MagicMock()
    cmd.insert.return_value.execute.return_value.data = [{"id": cmd_id}]
    cmd.select.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
        {"payload": {"sha256": s}} for s in (inflight_shas or [])]
    fake_sb.table.side_effect = lambda name: {"devices": dev, "commands": cmd}[name]
    return cmd


def _png(color=(255, 255, 255)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (320, 240), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def fake_r2(monkeypatch: pytest.MonkeyPatch) -> dict:
    """R2 스텁: put/delete/list/presign 을 메모리 dict 로. 실제 boto3 없음."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(lcd_router, "put_object_bytes",
                        lambda key, data, content_type="video/mp4": store.__setitem__(key, data))
    monkeypatch.setattr(lcd_router, "delete_object", lambda key: store.pop(key, None))
    monkeypatch.setattr(lcd_router, "list_objects",
                        lambda prefix: [(k, None) for k in list(store) if k.startswith(prefix)])
    monkeypatch.setattr(lcd_router, "generate_presigned_get_url",
                        lambda key, expires_in=3600: f"https://r2.test/get/{key}?sig=test")
    return store


def test_lcd_power_off_queues_command(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    res = app_client.put(f"/devices/{DEVICE_UUID}/lcd/power", json={"on": False})
    assert res.status_code == 201, res.text
    assert res.json()["action"] == "lcd_power"
    row = cmd.insert.call_args.args[0]
    assert row["payload"] == {"on": False} and row["ttl_sec"] == 30


def test_lcd_stage_n_requires_capability(app_client: TestClient, fake_sb: MagicMock) -> None:
    """구 펌웨어(capabilities 에 lcd_saver 없음)는 발행 전에 400 — unknown_action 을 기다리게 하지 않음."""
    cmd = _wire(fake_sb, _saver_device_mock(caps={"board": "mosfet"}))
    for method, path, body in (
        ("put", "lcd/power", {"on": True}),
        ("put", "lcd/quiet", {"start": "23:00", "end": "07:00"}),
        ("delete", "lcd/quiet", None),
        ("put", "lcd/saver", {"enabled": True}),
        ("post", "lcd/image", "file"),
        ("delete", "lcd/image", None),
    ):
        if body == "file":
            kwargs = {"files": {"file": ("a.png", _png(), "image/png")}}
        else:
            kwargs = {"json": body} if body is not None else {}
        res = getattr(app_client, method)(f"/devices/{DEVICE_UUID}/{path}", **kwargs)
        assert res.status_code == 400, (path, res.text)
        assert "unsupported_firmware" in res.json()["detail"]
    cmd.insert.assert_not_called()
    # 기존 Stage I 텍스트는 게이트 없음
    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd", json={"text": "hi"})
    assert res.status_code == 201, res.text


def test_lcd_quiet_overnight_window(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    res = app_client.put(f"/devices/{DEVICE_UUID}/lcd/quiet", json={"start": "23:30", "end": "07:00"})
    assert res.status_code == 201, res.text
    assert res.json()["action"] == "lcd_quiet"
    assert cmd.insert.call_args.args[0]["payload"] == {
        "start_min": 23 * 60 + 30, "end_min": 7 * 60, "tz_offset_min": 540,
    }


def test_lcd_quiet_rejects_bad_time_and_zero_window(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/quiet",
                          json={"start": "25:00", "end": "07:00"}).status_code == 400
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/quiet",
                          json={"start": "7", "end": "07:00"}).status_code == 400
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/quiet",
                          json={"start": "07:00", "end": "07:00"}).status_code == 400
    cmd.insert.assert_not_called()


def test_lcd_quiet_clear(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    res = app_client.delete(f"/devices/{DEVICE_UUID}/lcd/quiet")
    assert res.status_code == 201 and res.json()["action"] == "lcd_quiet_clear"
    assert cmd.insert.call_args.args[0]["payload"] is None


def test_lcd_saver_defaults_and_range(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    res = app_client.put(f"/devices/{DEVICE_UUID}/lcd/saver", json={"enabled": True})
    assert res.status_code == 201, res.text
    assert cmd.insert.call_args.args[0]["payload"] == {
        "enabled": True, "mode": "rotate", "image_sec": 20, "info_sec": 10, "show_offline_icon": True,
    }
    # 사진만 모드
    res = app_client.put(f"/devices/{DEVICE_UUID}/lcd/saver", json={"enabled": True, "mode": "image"})
    assert res.status_code == 201 and cmd.insert.call_args.args[0]["payload"]["mode"] == "image"
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/saver",
                          json={"enabled": True, "mode": "slideshow"}).status_code == 422
    # 범위 밖 → 422 (Pydantic)
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/saver",
                          json={"enabled": True, "image_sec": 1}).status_code == 422
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/saver",
                          json={"enabled": True, "info_sec": 301}).status_code == 422


def test_lcd_image_upload_r2_and_command(app_client: TestClient, fake_sb: MagicMock, fake_r2: dict) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd/image",
                          files={"file": ("photo.png", _png((255, 0, 0)), "image/png")})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["action"] == "lcd_image" and body["preview_url"].startswith("https://r2.test/get/lcd/")
    sha = body["sha256"]
    key = f"lcd/{DEVICE_UUID}/{sha[:8]}"
    # R2 에 기기용 + 미리보기 두 객체
    assert len(fake_r2[f"{key}.rgb565"]) == 40960 and fake_r2[f"{key}.png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert hashlib.sha256(fake_r2[f"{key}.rgb565"]).hexdigest() == sha
    # 명령 payload = 서버 경유 다운로드 URL + 검증 재료 (비트맵 본문은 MQTT 로 안 감)
    payload = cmd.insert.call_args.args[0]["payload"]
    assert payload["url"].endswith(f"/devices/{DEVICE_UUID}/lcd/image/bin?sha={sha[:8]}")
    assert payload["size"] == 40960 and payload["sha256"] == sha
    assert payload["w"] == 160 and payload["h"] == 128 and payload["enc"] == "rgb565"
    assert "data" not in payload


def test_lcd_image_upload_cleans_orphans_but_keeps_confirmed(
    app_client: TestClient, fake_sb: MagicMock, fake_r2: dict
) -> None:
    """이전에 올렸지만 기기가 ack 안 한 찌꺼기는 지우고, 확정된(lcd_image_key) 사진은 남긴다."""
    dev = _saver_device_mock()
    dev.select.return_value.eq.return_value.limit.return_value.execute.return_value.data[0]["lcd_image_key"] = \
        f"lcd/{DEVICE_UUID}/confirmd"
    _wire(fake_sb, dev)
    fake_r2[f"lcd/{DEVICE_UUID}/confirmd.rgb565"] = b"x"
    fake_r2[f"lcd/{DEVICE_UUID}/confirmd.png"] = b"y"
    fake_r2[f"lcd/{DEVICE_UUID}/orphan00.rgb565"] = b"z"
    fake_r2[f"lcd/{DEVICE_UUID}/orphan00.png"] = b"z"
    fake_r2["lcd/other-dev/aaaaaaaa.png"] = b"o"
    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd/image",
                          files={"file": ("p.png", _png(), "image/png")})
    assert res.status_code == 201, res.text
    assert f"lcd/{DEVICE_UUID}/confirmd.png" in fake_r2
    assert f"lcd/{DEVICE_UUID}/orphan00.png" not in fake_r2
    assert "lcd/other-dev/aaaaaaaa.png" in fake_r2


def test_lcd_image_upload_keeps_inflight_command_image(
    app_client: TestClient, fake_sb: MagicMock, fake_r2: dict
) -> None:
    """연속 업로드: 직전 업로드의 lcd_image 명령이 아직 pending/sent 면(기기가 내려받는 중일 수 있음)
    그 사진은 지우지 않는다 — 지우면 기기가 404(http_404) 를 받는다."""
    inflight = "ef" * 32
    _wire(fake_sb, _saver_device_mock(), inflight_shas=[inflight])
    fake_r2[f"lcd/{DEVICE_UUID}/efefefef.rgb565"] = b"x"
    fake_r2[f"lcd/{DEVICE_UUID}/efefefef.png"] = b"y"
    fake_r2[f"lcd/{DEVICE_UUID}/orphan00.rgb565"] = b"z"
    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd/image",
                          files={"file": ("p.png", _png(), "image/png")})
    assert res.status_code == 201, res.text
    assert f"lcd/{DEVICE_UUID}/efefefef.rgb565" in fake_r2
    assert f"lcd/{DEVICE_UUID}/efefefef.png" in fake_r2
    assert f"lcd/{DEVICE_UUID}/orphan00.rgb565" not in fake_r2


def test_lcd_image_bad_inputs_400(app_client: TestClient, fake_sb: MagicMock, fake_r2: dict) -> None:
    cmd = _wire(fake_sb, _saver_device_mock())
    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd/image",
                          files={"file": ("x.bin", b"not an image", "application/octet-stream")})
    assert res.status_code == 400, res.text
    cmd.insert.assert_not_called()
    assert not fake_r2


def test_lcd_image_r2_not_configured_503(app_client: TestClient, fake_sb: MagicMock, monkeypatch) -> None:
    from backend.r2_client import R2NotConfigured
    cmd = _wire(fake_sb, _saver_device_mock())

    def _boom(*a, **k):
        raise R2NotConfigured("no r2")
    monkeypatch.setattr(lcd_router, "put_object_bytes", _boom)
    res = app_client.post(f"/devices/{DEVICE_UUID}/lcd/image",
                          files={"file": ("p.png", _png(), "image/png")})
    assert res.status_code == 503
    cmd.insert.assert_not_called()


def test_lcd_image_bin_download_requires_device_token(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch
) -> None:
    """펌웨어 다운로드: Bearer 기기 토큰 검증 → R2 스트리밍 + Content-Length."""
    from backend.auth_device import DeviceAuthError

    class _Body:
        def __init__(self, data: bytes): self._d = data
        def iter_chunks(self, n: int):
            for i in range(0, len(self._d), n):
                yield self._d[i:i + n]
        def close(self): pass

    class _Client:
        def head_object(self, Bucket, Key):
            if not Key.endswith("/abcdef01.rgb565"):
                raise RuntimeError("NoSuchKey")
            return {"ContentLength": 40960}
        def get_object(self, Bucket, Key):
            return {"Body": _Body(b"\xab" * 40960)}

    monkeypatch.setattr(lcd_router, "get_r2_client", lambda: _Client())
    monkeypatch.setattr(lcd_router, "get_r2_bucket", lambda: "b")
    seen: dict = {}

    def _verify(kind, uuid, token):
        seen.update(kind=kind, uuid=uuid, token=token)
        if token != "dev-token":
            raise DeviceAuthError("bad")
        return {"id": uuid}
    monkeypatch.setattr(lcd_router, "verify_entity_token", _verify)

    assert app_client.get(f"/devices/{DEVICE_UUID}/lcd/image/bin?sha=abcdef01").status_code == 401
    assert app_client.get(f"/devices/{DEVICE_UUID}/lcd/image/bin?sha=abcdef01",
                          headers={"Authorization": "Bearer wrong"}).status_code == 401
    res = app_client.get(f"/devices/{DEVICE_UUID}/lcd/image/bin?sha=abcdef01",
                         headers={"Authorization": "Bearer dev-token"})
    assert res.status_code == 200, res.text
    assert seen == {"kind": "device", "uuid": DEVICE_UUID, "token": "dev-token"}
    assert res.headers["content-length"] == "40960" and len(res.content) == 40960
    assert app_client.get(f"/devices/{DEVICE_UUID}/lcd/image/bin?sha=00000000",
                          headers={"Authorization": "Bearer dev-token"}).status_code == 404
    assert app_client.get(f"/devices/{DEVICE_UUID}/lcd/image/bin?sha=ZZ",
                          headers={"Authorization": "Bearer dev-token"}).status_code == 422


def test_lcd_image_clear(app_client: TestClient, fake_sb: MagicMock) -> None:
    _wire(fake_sb, _saver_device_mock())
    res = app_client.delete(f"/devices/{DEVICE_UUID}/lcd/image")
    assert res.status_code == 201 and res.json()["action"] == "lcd_image_clear"


def test_lcd_get_state(app_client: TestClient, fake_sb: MagicMock, fake_r2: dict) -> None:
    dev = MagicMock()
    dev.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{
        "id": DEVICE_UUID, "owner_id": TEST_USER_ID, "capabilities": {"lcd_saver": True},
        "lcd_text": "밥 6시", "lcd_power": False, "lcd_quiet_start": "23:00:00",
        "lcd_quiet_end": "07:00:00", "lcd_saver": {"enabled": True, "image_sec": 20, "info_sec": 10},
        "lcd_image_key": f"lcd/{DEVICE_UUID}/abcdef01",
        "lcd_image_set_at": "2026-10-28T00:00:00+00:00", "lcd_updated_at": "2026-10-28T00:00:00+00:00",
    }]
    fake_sb.table.side_effect = lambda name: {"devices": dev}[name]
    res = app_client.get(f"/devices/{DEVICE_UUID}/lcd")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["device_id"] == DEVICE_UUID and body["lcd_power"] is False
    assert body["lcd_quiet_start"] == "23:00:00" and body["lcd_saver"]["image_sec"] == 20
    assert body["preview_url"] == f"https://r2.test/get/lcd/{DEVICE_UUID}/abcdef01.png?sig=test"
    assert "owner_id" not in body


def test_lcd_stage_n_foreign_device_404(app_client: TestClient, fake_sb: MagicMock) -> None:
    cmd = _wire(fake_sb, _saver_device_mock(owner="other-user"))
    assert app_client.put(f"/devices/{DEVICE_UUID}/lcd/power", json={"on": True}).status_code == 404
    assert app_client.get(f"/devices/{DEVICE_UUID}/lcd").status_code == 404
    cmd.insert.assert_not_called()
