"""WebRTC signaling API tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
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


def test_webrtc_config_turn_short_term_credential(app_client: TestClient, monkeypatch) -> None:
    """WEBRTC_TURN_SECRET 이 있으면 coturn use-auth-secret 형식의 단기 자격증명을 만든다.

    username = "<만료ts>:<user_id>", credential = base64(HMAC-SHA1(secret, username)).
    coturn 이 같은 계산으로 대조하므로 형식이 한 글자라도 다르면 relay 가 전부 401 이다.
    """
    urls = 'turn:turn.example.com:3478?transport=udp,turns:turn.example.com:443?transport=tcp'
    monkeypatch.setenv('WEBRTC_TURN_URLS', urls)
    monkeypatch.setenv('WEBRTC_TURN_SECRET', 'test-secret')
    monkeypatch.setenv('WEBRTC_TURN_TTL_SEC', '3600')
    before = int(time.time())

    res = app_client.get('/cameras/webrtc/config')

    assert res.status_code == 200
    turn = res.json()['iceServers'][1]
    assert turn['urls'] == urls.split(',')
    exp_s, uid = turn['username'].split(':', 1)
    assert uid == TEST_USER_ID
    assert before + 3600 <= int(exp_s) <= before + 3600 + 5
    expected = base64.b64encode(
        hmac.new(b'test-secret', turn['username'].encode(), hashlib.sha1).digest()
    ).decode()
    assert turn['credential'] == expected


def test_webrtc_config_turn_static_credential_fallback(app_client: TestClient, monkeypatch) -> None:
    """SECRET 이 없고 USERNAME/CREDENTIAL 만 있으면 종전처럼 정적 자격증명(개발용)."""
    monkeypatch.setenv('WEBRTC_TURN_URLS', 'turn:turn.example.com:3478')
    monkeypatch.delenv('WEBRTC_TURN_SECRET', raising=False)
    monkeypatch.setenv('WEBRTC_TURN_USERNAME', 'terra')
    monkeypatch.setenv('WEBRTC_TURN_CREDENTIAL', 'pw')

    turn = app_client.get('/cameras/webrtc/config').json()['iceServers'][1]
    assert turn['username'] == 'terra' and turn['credential'] == 'pw'


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


def test_webrtc_offer_reports_single_attempt(
    app_client: TestClient,
    fake_sb: MagicMock,
    monkeypatch,
) -> None:
    """첫 시도에 answer 가 오면 offer_attempts=1 — 앱이 ICE/NAT 문제로 판별하는 근거."""
    _setup_owned_camera(fake_sb)

    class FakeSignaling:
        def request_answer(self, camera_id, command, *, session_id, timeout_sec):
            return {'action': 'webrtc_answer', 'session_id': session_id, 'sdp': 'answer-sdp'}

    from backend.routers import webrtc as webrtc_router

    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)

    res = app_client.post(
        f'/cameras/{CAMERA_UUID}/webrtc/offer',
        json={'sdp': 'offer-sdp', 'session_id': 'sess-once', 'timeout_sec': 2},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body['offer_attempts'] == 1
    assert body['answer_ms'] >= 0


def test_webrtc_offer_reports_retry_count(
    app_client: TestClient,
    fake_sb: MagicMock,
    monkeypatch,
) -> None:
    """첫 offer 가 무응답이고 두 번째에 붙으면 offer_attempts=2.

    이 값이 앱 로그(webrtc_connect_logs)에서 '펌웨어가 첫 offer 에 답을 못 했다'를
    가리킨다 — esp_peer_open PSRAM 경합. 앱은 이걸 스스로 알 수 없다.
    """
    _setup_owned_camera(fake_sb)
    calls: list[str] = []

    class FakeSignaling:
        def request_answer(self, camera_id, command, *, session_id, timeout_sec):
            calls.append(command['msg_id'])
            if len(calls) == 1:
                raise WebRTCSignalingTimeout('no answer')
            return {'action': 'webrtc_answer', 'session_id': session_id, 'sdp': 'answer-sdp'}

    from backend.routers import webrtc as webrtc_router

    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)

    res = app_client.post(
        f'/cameras/{CAMERA_UUID}/webrtc/offer',
        json={'sdp': 'offer-sdp', 'session_id': 'sess-retry', 'timeout_sec': 2},
    )

    assert res.status_code == 200, res.text
    assert res.json()['offer_attempts'] == 2
    # 재발행마다 msg_id 가 달라야 펌웨어가 중복으로 무시하지 않는다
    assert len(calls) == 2 and calls[0] != calls[1]


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
