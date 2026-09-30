"""WebRTC offer 시간당 안전망 (설계 P4, owner 결정 09-29: 앱 백오프 문서 + 카메라당 시간당 상한).

7일 실측(webrtc_connect_logs): 현장 B 81회/시 vs 나머지 27대 최대 38회/시. 분·10분 창으로는 구분이
안 돼 1시간 창만 쓴다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend import webrtc_offer_guard
from backend.webrtc_offer_guard import OfferRateLimiter
from tests.test_webrtc_api import CAMERA_UUID, _setup_owned_camera

OTHER_CAMERA = '44444444-4444-4444-4444-444444444444'


@pytest.fixture(autouse=True)
def _fresh_limiter():
    webrtc_offer_guard.get_offer_limiter.cache_clear()
    yield
    webrtc_offer_guard.get_offer_limiter.cache_clear()


# ---------- 단위 ----------


def test_limiter_allows_up_to_limit_then_reports_retry_after() -> None:
    lim = OfferRateLimiter(limit=3, window_sec=3600)
    assert [lim.check('cam', now=t) for t in (0, 10, 20)] == [None, None, None]
    retry = lim.check('cam', now=30)
    assert retry == 3600 - 30        # 가장 오래된 시도(0초)가 창을 벗어날 때까지


def test_limiter_window_slides() -> None:
    lim = OfferRateLimiter(limit=2, window_sec=3600)
    lim.check('cam', now=0)
    lim.check('cam', now=100)
    assert lim.check('cam', now=200) is not None
    assert lim.check('cam', now=3601) is None          # 0초 시도가 빠짐


def test_rejected_attempts_do_not_extend_the_block() -> None:
    """429 로 거절된 요청은 기록하지 않는다 — 앱이 계속 두드려도 차단이 영원히 연장되지 않게."""
    lim = OfferRateLimiter(limit=1, window_sec=3600)
    lim.check('cam', now=0)
    for t in range(1, 3600, 60):
        assert lim.check('cam', now=t) is not None
    assert lim.check('cam', now=3601) is None


def test_limiter_is_per_camera() -> None:
    lim = OfferRateLimiter(limit=1, window_sec=3600)
    assert lim.check('a', now=0) is None
    assert lim.check('b', now=0) is None


def test_zero_limit_disables() -> None:
    lim = OfferRateLimiter(limit=0, window_sec=3600)
    assert all(lim.check('cam', now=t) is None for t in range(200))


def test_env_configures_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('WEBRTC_OFFER_LIMIT_PER_HOUR', '5')
    assert webrtc_offer_guard.get_offer_limiter().limit == 5


def test_default_limit_is_60(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('WEBRTC_OFFER_LIMIT_PER_HOUR', raising=False)
    assert webrtc_offer_guard.get_offer_limiter().limit == 60


def test_bad_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('WEBRTC_OFFER_LIMIT_PER_HOUR', 'lots')
    assert webrtc_offer_guard.get_offer_limiter().limit == 60


# ---------- API ----------


def _fake_signaling(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    class FakeSignaling:
        def request_answer(self, camera_id, command, *, session_id, timeout_sec):
            calls.append(session_id)
            return {'action': 'webrtc_answer', 'session_id': session_id, 'sdp': 'answer-sdp'}

    from backend.routers import webrtc as webrtc_router
    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)
    return calls


def _offer(app_client: TestClient, camera: str, i: int):
    return app_client.post(f'/cameras/{camera}/webrtc/offer',
                           json={'sdp': 'offer-sdp', 'session_id': f's-{i}', 'timeout_sec': 2})


def test_offer_over_hourly_limit_returns_429_without_touching_camera(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('WEBRTC_OFFER_LIMIT_PER_HOUR', '3')
    _setup_owned_camera(fake_sb)
    calls = _fake_signaling(monkeypatch)

    codes = [_offer(app_client, CAMERA_UUID, i).status_code for i in range(3)]
    blocked = _offer(app_client, CAMERA_UUID, 99)

    assert codes == [200, 200, 200]
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers['Retry-After']) <= 3600
    assert calls == ['s-0', 's-1', 's-2']          # 차단된 요청은 카메라에 offer 를 안 보낸다


def test_limit_is_counted_per_camera_in_api(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('WEBRTC_OFFER_LIMIT_PER_HOUR', '1')
    _setup_owned_camera(fake_sb)
    _fake_signaling(monkeypatch)

    assert _offer(app_client, CAMERA_UUID, 0).status_code == 200
    assert _offer(app_client, OTHER_CAMERA, 1).status_code == 200
    assert _offer(app_client, CAMERA_UUID, 2).status_code == 429


def test_not_owned_camera_does_not_consume_budget(
    app_client: TestClient, fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """소유권 검증(404) 전에는 세지 않는다 — 남이 내 카메라 예산을 소진할 수 없게."""
    monkeypatch.setenv('WEBRTC_OFFER_LIMIT_PER_HOUR', '1')
    _fake_signaling(monkeypatch)
    _setup_owned_camera(fake_sb, row={})              # 미존재 → 404
    assert _offer(app_client, CAMERA_UUID, 0).status_code == 404
    _setup_owned_camera(fake_sb)
    assert _offer(app_client, CAMERA_UUID, 1).status_code == 200
