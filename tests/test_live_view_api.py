"""라이브 시청 제한 — WebRTC 라우터 연결 (live_session 로직은 test_live_session.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend import live_session, webrtc_offer_guard
from backend.live_session import Claim, LiveBusy, LiveCooldown
from backend.webrtc_signaling import WebRTCSignalingTimeout
from tests.conftest import TEST_USER_ID
from tests.test_webrtc_api import CAMERA_TEXT, CAMERA_UUID, _setup_owned_camera

UNTIL = datetime(2026, 9, 30, 12, 15, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_limiter():
    webrtc_offer_guard.get_offer_limiter.cache_clear()
    yield
    webrtc_offer_guard.get_offer_limiter.cache_clear()


@pytest.fixture
def signaling(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    log: dict[str, list] = {"offers": [], "published": []}

    class FakeSignaling:
        timeout = False

        def request_answer(self, camera_id, command, *, session_id, timeout_sec):
            log["offers"].append(session_id)
            if FakeSignaling.timeout:
                raise WebRTCSignalingTimeout("no answer")
            return {'action': 'webrtc_answer', 'session_id': session_id, 'sdp': 'answer-sdp'}

        def publish(self, camera_id, command, **_kw):
            log["published"].append((camera_id, command["action"], command["session_id"]))

    from backend.routers import webrtc as webrtc_router
    monkeypatch.setattr(webrtc_router, 'MqttWebRTCSignaling', FakeSignaling)
    log["cls"] = [FakeSignaling]
    return log


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    rec: dict[str, list] = {"claim": [], "release": []}
    state: dict[str, Any] = {"claim": Claim(previous_session=None, live_until=UNTIL)}

    def _claim(sb, camera_uuid, owner_id, **kw):
        rec["claim"].append({"camera": camera_uuid, "owner": owner_id, **kw})
        result = state["claim"]
        if isinstance(result, Exception):
            raise result
        return result

    def _release(sb, camera_uuid, owner_id, session_id, reason, now=None):
        rec["release"].append((camera_uuid, owner_id, session_id, reason))
        return True

    monkeypatch.setattr(live_session, 'claim', _claim)
    monkeypatch.setattr(live_session, 'release', _release)
    rec["state"] = [state]
    return rec


def _offer(app_client: TestClient, **extra: Any):
    body = {'sdp': 'offer-sdp', 'session_id': 'sess-new', 'timeout_sec': 2, **extra}
    return app_client.post(f'/cameras/{CAMERA_UUID}/webrtc/offer', json=body)


def test_offer_claims_session_with_viewer_and_returns_live_until(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    _setup_owned_camera(fake_sb)
    res = _offer(app_client, viewer_id='dev-a', viewer_label='iPhone 15')

    assert res.status_code == 200, res.text
    assert res.json()['live_until'] == UNTIL.isoformat()
    c = calls["claim"][0]
    assert (c["camera"], c["owner"], c["session_id"]) == (CAMERA_UUID, TEST_USER_ID, 'sess-new')
    assert (c["viewer_id"], c["viewer_label"], c["takeover"]) == ('dev-a', 'iPhone 15', False)


def test_legacy_offer_without_viewer_fields_still_works(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    _setup_owned_camera(fake_sb)
    assert _offer(app_client).status_code == 200
    assert calls["claim"][0]["viewer_id"] is None


def test_busy_returns_409_with_viewer_and_does_not_touch_camera(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    _setup_owned_camera(fake_sb)
    calls["state"][0]["claim"] = LiveBusy("Galaxy S24", "2026-09-30T12:00:00+00:00")

    res = _offer(app_client, viewer_id='dev-a', viewer_label='iPhone 15')

    assert res.status_code == 409
    detail = res.json()['detail']
    assert detail['code'] == 'live_in_use'
    assert detail['viewer'] == 'Galaxy S24'
    assert signaling["offers"] == [] and signaling["published"] == []


def test_cooldown_returns_429_with_retry_after(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    _setup_owned_camera(fake_sb)
    calls["state"][0]["claim"] = LiveCooldown(240)

    res = _offer(app_client)

    assert res.status_code == 429
    assert res.json()['detail']['code'] == 'live_cooldown'
    assert res.json()['detail']['retry_after'] == 240
    assert res.headers['Retry-After'] == '240'
    assert signaling["offers"] == []


def test_takeover_closes_previous_session_before_offer(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    _setup_owned_camera(fake_sb)
    calls["state"][0]["claim"] = Claim(previous_session='sess-old', live_until=UNTIL)

    res = _offer(app_client, viewer_id='dev-b', viewer_label='Galaxy S24', takeover=True)

    assert res.status_code == 200, res.text
    assert calls["claim"][0]["takeover"] is True
    assert signaling["published"] == [(CAMERA_TEXT, 'webrtc_close', 'sess-old')]
    assert signaling["offers"] == ['sess-new']


def test_offer_timeout_releases_claim(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    _setup_owned_camera(fake_sb)
    signaling["cls"][0].timeout = True

    assert _offer(app_client).status_code == 504
    assert calls["release"] == [(CAMERA_UUID, TEST_USER_ID, 'sess-new', 'failed')]


def test_rate_limit_is_structured_429(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간당 안전망(#12) 429 도 앱이 구분할 수 있게 code 를 싣는다."""
    monkeypatch.setenv('WEBRTC_OFFER_LIMIT_PER_HOUR', '1')
    _setup_owned_camera(fake_sb)
    assert _offer(app_client).status_code == 200
    res = _offer(app_client)
    assert res.status_code == 429
    assert res.json()['detail']['code'] == 'rate_limited'


def test_close_releases_only_this_session(
    app_client: TestClient, fake_sb: MagicMock, signaling: dict, calls: dict,
) -> None:
    """close 는 자기 세션일 때만 정리 — 가져오기 당한 기기의 늦은 close 가 새 시청자를 지우면 안 된다."""
    _setup_owned_camera(fake_sb)
    res = app_client.post(f'/cameras/{CAMERA_UUID}/webrtc/close', json={'session_id': 'sess-old'})

    assert res.status_code == 200
    assert calls["release"] == [(CAMERA_UUID, TEST_USER_ID, 'sess-old', 'closed')]
    fake_sb.table.return_value.update.assert_not_called()
