"""WebRTC signaling API tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.webrtc_signaling import WebRTCSignalingTimeout
from tests.conftest import TEST_USER_ID


CAMERA_UUID = '33333333-3333-3333-3333-333333333333'
CAMERA_TEXT = 'p4cam-aabbccdd'


def _camera_row(**overrides: object) -> dict:
    row = {
        'id': CAMERA_UUID,
        'owner_id': TEST_USER_ID,
        'camera_id': CAMERA_TEXT,
        'stream_mode': None,
        'stream_until': None,
    }
    row.update(overrides)
    return row


def _setup_owned_camera(fake_sb: MagicMock, row: dict | None = None) -> None:
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value
    chain.execute.return_value.data = row if row is not None else _camera_row()


def test_webrtc_config_returns_default_stun(app_client: TestClient) -> None:
    res = app_client.get('/cameras/webrtc/config')
    assert res.status_code == 200
    body = res.json()
    assert body['iceServers'][0]['urls'] == ['stun:stun.l.google.com:19302']
    assert body['sdpSemantics'] == 'unified-plan'


def test_webrtc_offer_relays_to_camera_and_returns_answer(
    app_client: TestClient,
    fake_sb: MagicMock,
    monkeypatch,
) -> None:
    _setup_owned_camera(fake_sb)
    captured: dict = {}

    class FakeSignaling:
        def request_answer(self, camera_id, command, *, session_id, timeout_sec):
            captured.update({
                'camera_id': camera_id,
                'command': command,
                'session_id': session_id,
                'timeout_sec': timeout_sec,
            })
            return {'action': 'webrtc_answer', 'session_id': session_id, 'sdp': 'answer-sdp'}

    from backend.routers import webrtc as webrtc_router

    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)

    res = app_client.post(
        f'/cameras/{CAMERA_UUID}/webrtc/offer',
        json={'sdp': 'offer-sdp', 'session_id': 'sess-1', 'timeout_sec': 2},
    )

    assert res.status_code == 200, res.text
    assert res.json()['sdp'] == 'answer-sdp'
    assert captured['camera_id'] == CAMERA_TEXT
    assert captured['command']['action'] == 'webrtc_offer'
    assert captured['command']['sdp'] == 'offer-sdp'
    assert captured['command']['session_id'] == 'sess-1'


def test_webrtc_offer_timeout_returns_504(
    app_client: TestClient,
    fake_sb: MagicMock,
    monkeypatch,
) -> None:
    _setup_owned_camera(fake_sb)

    class FakeSignaling:
        def request_answer(self, camera_id, command, *, session_id, timeout_sec):
            raise WebRTCSignalingTimeout('camera WebRTC answer timed out')

    from backend.routers import webrtc as webrtc_router

    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)

    res = app_client.post(
        f'/cameras/{CAMERA_UUID}/webrtc/offer',
        json={'sdp': 'offer-sdp', 'session_id': 'sess-timeout', 'timeout_sec': 1},
    )

    assert res.status_code == 504


def test_webrtc_ice_publishes_command(
    app_client: TestClient,
    fake_sb: MagicMock,
    monkeypatch,
) -> None:
    _setup_owned_camera(fake_sb)
    published: dict = {}

    class FakeSignaling:
        def publish(self, camera_id, command, *, timeout_sec=5.0):
            published.update({'camera_id': camera_id, 'command': command})

    from backend.routers import webrtc as webrtc_router

    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)

    candidate = {'candidate': 'candidate:1 1 udp 1 127.0.0.1 9 typ host', 'sdpMid': '0'}
    res = app_client.post(
        f'/cameras/{CAMERA_UUID}/webrtc/ice',
        json={'session_id': 'sess-ice', 'candidate': candidate},
    )

    assert res.status_code == 200, res.text
    assert res.json() == {'ok': True, 'session_id': 'sess-ice'}
    assert published['camera_id'] == CAMERA_TEXT
    assert published['command']['action'] == 'webrtc_ice'
    assert published['command']['candidate'] == candidate
