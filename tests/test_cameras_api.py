"""cameras 라우터 (pair + CRUD) 통합 테스트."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tests.conftest import TEST_USER_ID


def _camera_row(**overrides: object) -> dict:
    base = {
        "id": "cam-uuid",
        "owner_id": TEST_USER_ID,
        "camera_id": "p4cam-aabbccdd",
        "enclosure_id": None,
        "name": "거실 카메라",
        "model": "esp32-p4",
        "firmware_ver": "0.1.0",
        "resolution": "HD",
        "fps": 24,
        "clip_sec": 10,
        "stream_mode": None,
        "stream_until": None,
        "created_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
        "last_seen_at": None,
        "is_online": False,
    }
    base.update(overrides)
    return base


def test_pair_camera_success(app_client: TestClient, fake_sb: MagicMock) -> None:
    inserted = _camera_row()
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [inserted]

    res = app_client.post(
        "/cameras/pair",
        json={"name": "거실 카메라", "model": "esp32-p4"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == "cam-uuid"
    assert body["camera_id"].startswith("p4cam-")
    # 평문 토큰이 1회 응답에 노출됨
    assert len(body["camera_token"]) > 20

    # MQTT 접속 정보: env 그대로 전달 (펌웨어가 NVS 저장 후 바로 connect)
    assert body["mqtt_broker_host"] == "test-broker.local"
    assert body["mqtt_broker_port"] == 8883
    assert body["mqtt_use_tls"] is True

    # INSERT payload: token 은 평문 아닌 bcrypt hash
    insert_call = fake_sb.table.return_value.insert.call_args
    payload = insert_call.args[0]
    assert payload["token_hash"] != body["camera_token"]
    assert payload["token_hash"].startswith("$2")  # bcrypt prefix
    assert payload["owner_id"] == TEST_USER_ID


def _hw_lookup(fake_sb: MagicMock):
    """/cameras/pair 의 hw_id 조회 체인 (select→eq→eq→is_→limit→execute)."""
    return (
        fake_sb.table.return_value.select.return_value
        .eq.return_value.eq.return_value.is_.return_value.limit.return_value.execute.return_value
    )


def test_pair_camera_new_hw_id_inserts_and_stores_it(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """처음 보는 hw_id → 기존대로 새 행 생성. hw_id 가 INSERT payload 에 들어간다."""
    _hw_lookup(fake_sb).data = []          # 같은 보드 없음
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [_camera_row()]

    res = app_client.post(
        "/cameras/pair",
        json={"name": "거실 카메라", "model": "esp32-p4", "hw_id": "30EDA0E22E80"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["reused"] is False

    payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert payload["hw_id"] == "30EDA0E22E80"


def test_pair_camera_same_hw_id_reuses_row_instead_of_inserting(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """같은 보드 재페어링 → 새 행 없이 기존 행 갱신. 중복 카메라가 생기지 않는다."""
    _hw_lookup(fake_sb).data = [{"id": "cam-uuid", "camera_id": "p4cam-aabbccdd"}]
    fake_sb.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        _camera_row()
    ]

    res = app_client.post(
        "/cameras/pair",
        json={"name": "거실 카메라", "model": "esp32-p4", "hw_id": "30EDA0E22E80"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["reused"] is True
    assert body["camera_id"] == "p4cam-aabbccdd"   # camera_id 는 종전 값 유지
    assert len(body["camera_token"]) > 20          # 토큰은 새로 발급

    fake_sb.table.return_value.insert.assert_not_called()

    # 새 토큰의 hash 로 갱신 — 평문이 저장되지 않는다
    patch = fake_sb.table.return_value.update.call_args.args[0]
    assert patch["token_hash"].startswith("$2")
    assert patch["token_hash"] != body["camera_token"]


def test_pair_camera_reuse_keeps_enclosure_when_not_given(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """enclosure_id 미지정 재페어링이 기존 사육장 연결을 끊지 않는다."""
    _hw_lookup(fake_sb).data = [{"id": "cam-uuid", "camera_id": "p4cam-aabbccdd"}]
    fake_sb.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        _camera_row()
    ]

    res = app_client.post(
        "/cameras/pair",
        json={"name": "거실 카메라", "hw_id": "30EDA0E22E80"},
    )
    assert res.status_code == 201, res.text
    patch = fake_sb.table.return_value.update.call_args.args[0]
    assert "enclosure_id" not in patch


def test_pair_camera_without_hw_id_never_reuses(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    """구 펌웨어(hw_id 없음) → 조회 자체를 안 하고 기존 동작(항상 새 행) 유지."""
    _hw_lookup(fake_sb).data = [{"id": "other", "camera_id": "p4cam-zzzz"}]
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [_camera_row()]

    res = app_client.post("/cameras/pair", json={"name": "거실 카메라"})
    assert res.status_code == 201, res.text
    assert res.json()["reused"] is False
    fake_sb.table.return_value.insert.assert_called_once()


def test_pair_camera_with_invalid_model(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    res = app_client.post(
        "/cameras/pair",
        json={"name": "x", "model": "unknown-model"},
    )
    assert res.status_code == 400


def test_pair_camera_with_invalid_resolution(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    res = app_client.post(
        "/cameras/pair",
        json={"name": "x", "resolution": "4K"},
    )
    assert res.status_code == 400


def test_pair_camera_with_other_users_enclosure(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    # enclosure_owner_verify 가 다른 유저 반환
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = {"owner_id": "other-user"}

    res = app_client.post(
        "/cameras/pair",
        json={"name": "x", "enclosure_id": "enc-99"},
    )
    assert res.status_code == 400


def test_pair_camera_rpi_model_prefix(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    inserted = _camera_row(model="rpi-zero-2-w", camera_id="picam-aabbccdd")
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [inserted]

    res = app_client.post(
        "/cameras/pair",
        json={"name": "거실", "model": "rpi-zero-2-w"},
    )
    assert res.status_code == 201
    # camera_id 가 picam- 접두사 (model 별 분기)
    insert_payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert insert_payload["camera_id"].startswith("picam-")


def test_list_cameras(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.order.return_value
    chain.execute.return_value.data = [_camera_row()]

    res = app_client.get("/cameras")
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_get_camera_not_owner_404(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = _camera_row(owner_id="other-user")

    res = app_client.get("/cameras/cam-uuid")
    assert res.status_code == 404


def test_get_camera_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = _camera_row()

    res = app_client.get("/cameras/cam-uuid")
    assert res.status_code == 200
    assert res.json()["camera_id"] == "p4cam-aabbccdd"


def test_update_camera_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value
    chain.execute.return_value.data = [_camera_row(name="새이름")]

    res = app_client.patch("/cameras/cam-uuid", json={"name": "새이름"})
    assert res.status_code == 200
    assert res.json()["name"] == "새이름"


def test_update_camera_invalid_resolution(
    app_client: TestClient, fake_sb: MagicMock
) -> None:
    res = app_client.patch("/cameras/cam-uuid", json={"resolution": "8K"})
    assert res.status_code == 400


def test_delete_camera_ok(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.delete.return_value.eq.return_value.eq.return_value
    # camera_id 도 포함 — registry.unregister_device 가 참조
    chain.execute.return_value.data = [{"id": "cam-uuid", "camera_id": "p4cam-aabbccdd"}]

    res = app_client.delete("/cameras/cam-uuid")
    assert res.status_code == 204


# ---------- rotate_180 / capabilities (2026-09-08 앱 핸드오프 rotate180) ----------


def _install_fake_signaling(monkeypatch, calls: list[tuple[str, dict]], *, raise_exc: bool = False):
    """cameras_router.MqttWebRTCSignaling 을 publish 캡처용 stub 으로 교체."""
    from backend.routers import cameras as cameras_router

    class _FakeSignaling:
        def __init__(self) -> None:
            if raise_exc:
                raise RuntimeError("MQTT env missing")

        def publish(self, camera_id: str, command: dict, **_: object) -> None:
            calls.append((camera_id, command))

    monkeypatch.setattr(cameras_router, "MqttWebRTCSignaling", _FakeSignaling)


def test_update_camera_rotate_180_publishes_set_rotation(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch
) -> None:
    calls: list[tuple[str, dict]] = []
    _install_fake_signaling(monkeypatch, calls)
    chain = fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value
    chain.execute.return_value.data = [_camera_row(rotate_180=True)]

    res = app_client.patch("/cameras/cam-uuid", json={"rotate_180": True})
    assert res.status_code == 200
    assert res.json()["rotate_180"] is True

    # DB UPDATE 에 rotate_180 포함
    update_payload = fake_sb.table.return_value.update.call_args.args[0]
    assert update_payload == {"rotate_180": True}

    # set_rotation 명령 1회, 계약 필드 그대로
    assert len(calls) == 1
    camera_id, cmd = calls[0]
    assert camera_id == "p4cam-aabbccdd"
    assert cmd["action"] == "set_rotation"
    assert cmd["rotate_180"] is True
    assert cmd["ttl_sec"] == 60
    assert "msg_id" in cmd and "issued_at" in cmd


def test_update_camera_without_rotate_does_not_publish(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch
) -> None:
    calls: list[tuple[str, dict]] = []
    _install_fake_signaling(monkeypatch, calls)
    chain = fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value
    chain.execute.return_value.data = [_camera_row(name="새이름")]

    res = app_client.patch("/cameras/cam-uuid", json={"name": "새이름"})
    assert res.status_code == 200
    assert calls == []


def test_update_camera_rotate_publish_failure_is_best_effort(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch
) -> None:
    """MQTT 발행 실패(env 누락 등)해도 DB 는 갱신됐으므로 200. 텔레메트리 동기화가 수렴."""
    calls: list[tuple[str, dict]] = []
    _install_fake_signaling(monkeypatch, calls, raise_exc=True)
    chain = fake_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value
    chain.execute.return_value.data = [_camera_row(rotate_180=False)]

    res = app_client.patch("/cameras/cam-uuid", json={"rotate_180": False})
    assert res.status_code == 200
    assert res.json()["rotate_180"] is False
    assert calls == []


def test_get_camera_capabilities_null_and_dict(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value

    # 구 펌웨어: 컬럼 NULL → null (앱은 토글 숨김)
    chain.execute.return_value.data = _camera_row()
    res = app_client.get("/cameras/cam-uuid")
    assert res.status_code == 200
    assert res.json()["capabilities"] is None
    assert res.json()["rotate_180"] is False   # 컬럼 없던 행도 기본 False

    # 신 펌웨어
    chain.execute.return_value.data = _camera_row(capabilities={"rotate_180": True}, rotate_180=True)
    res = app_client.get("/cameras/cam-uuid")
    assert res.json()["capabilities"] == {"rotate_180": True}
    assert res.json()["rotate_180"] is True


def test_get_camera_clip_stats_serialized(app_client: TestClient, fake_sb: MagicMock) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    stats = {"rec": 5, "skip": 1, "up_fail": 0, "sd_backlog": 2, "last_rec_s": 90, "up_busy_s": -1}
    chain.execute.return_value.data = _camera_row(clip_stats=stats, clip_stats_at="2026-09-16T01:00:00Z")
    res = app_client.get("/cameras/cam-uuid")
    assert res.status_code == 200
    assert res.json()["clip_stats"] == stats
    assert res.json()["clip_stats_at"] == "2026-09-16T01:00:00Z"
