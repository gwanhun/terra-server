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
    """cameras.select.eq(id).limit(1) 조회. 실 DB 처럼 요청한 id 를 row 의 id 로 돌려준다.

    row={} 면 0행 (미존재).
    """
    base = _camera_row() if row is None else row

    def _eq(_col: str, value: str) -> MagicMock:
        q = MagicMock()
        q.limit.return_value.execute.return_value.data = [{**base, 'id': value}] if base else []
        return q

    fake_sb.table.return_value.select.return_value.eq.side_effect = _eq


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


# ---------- ICE 후보 필터 (2026-10-21) ----------

def _cand(line: str) -> dict:
    return {'candidate': line, 'sdpMid': '0', 'sdpMLineIndex': 0}


def _ice_setup(app_client: TestClient, fake_sb: MagicMock, monkeypatch) -> list:
    _setup_owned_camera(fake_sb)
    published: list = []

    class FakeSignaling:
        def publish(self, camera_id, command, *, timeout_sec=5.0):
            published.append(command['candidate']['candidate'])

    from backend.routers import webrtc as webrtc_router

    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)
    webrtc_router._ice_sessions.clear()
    return published


def _post_ice(app_client: TestClient, session: str, line: str) -> None:
    res = app_client.post(
        f'/cameras/{CAMERA_UUID}/webrtc/ice',
        json={'session_id': session, 'candidate': _cand(line)},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {'ok': True, 'session_id': session}


def test_webrtc_ice_drops_candidates_camera_cannot_use(app_client, fake_sb, monkeypatch) -> None:
    published = _ice_setup(app_client, fake_sb, monkeypatch)
    for line in (
        'candidate:2 1 tcp 1518214911 192.168.0.5 9 typ host tcptype active',          # tcp
        'candidate:3 1 udp 2122260223 a1b2c3d4-0000.local 51234 typ host',              # mDNS
        'candidate:4 1 udp 2122262783 2001:db8::1 51235 typ host',                      # IPv6
        'candidate:5 1 udp 2122260223 169.254.10.2 51236 typ host',                     # link-local
        'candidate:6 1 udp 1845501695 203.0.113.9 51237 typ prflx raddr 0.0.0.0 rport 0',  # prflx
    ):
        _post_ice(app_client, 'sess-f', line)
    assert published == []
    _post_ice(app_client, 'sess-f', 'candidate:7 1 udp 41885439 3.37.22.197 60001 typ relay raddr 0.0.0.0 rport 0')
    assert len(published) == 1 and 'typ relay' in published[0]


def test_webrtc_ice_budget_keeps_room_for_relay(app_client, fake_sb, monkeypatch) -> None:
    published = _ice_setup(app_client, fake_sb, monkeypatch)
    for i in range(6):   # host 6개 → 3개만 전달
        _post_ice(app_client, 'sess-b', f'candidate:{i} 1 udp 2122260223 192.168.0.{10 + i} 5000{i} typ host')
    for i in range(3):   # srflx 3개 → 2개
        _post_ice(app_client, 'sess-b', f'candidate:{10 + i} 1 udp 1686052607 203.0.113.{i} 6000{i} typ srflx raddr 192.168.0.10 rport 50000')
    for i in range(4):   # relay 4개 → 3개 (host 가 먼저 많이 와도 relay 자리는 남는다)
        _post_ice(app_client, 'sess-b', f'candidate:{20 + i} 1 udp 41885439 3.37.22.197 7000{i} typ relay raddr 0.0.0.0 rport 0')
    assert len(published) == 8
    assert sum('typ host' in c for c in published) == 3
    assert sum('typ srflx' in c for c in published) == 2
    assert sum('typ relay' in c for c in published) == 3
    # 다른 세션은 별도 예산
    _post_ice(app_client, 'sess-c', 'candidate:1 1 udp 2122260223 192.168.0.99 50099 typ host')
    assert len(published) == 9


def test_webrtc_ice_passes_end_of_candidates_and_dedupes(app_client, fake_sb, monkeypatch) -> None:
    published = _ice_setup(app_client, fake_sb, monkeypatch)
    _post_ice(app_client, 'sess-d', 'candidate:1 1 udp 2122260223 192.168.0.5 50000 typ host')
    _post_ice(app_client, 'sess-d', 'candidate:1 1 udp 2122260223 192.168.0.5 50000 typ host')   # 중복
    _post_ice(app_client, 'sess-d', '')                                                          # end-of-candidates
    assert published == ['candidate:1 1 udp 2122260223 192.168.0.5 50000 typ host', '']
