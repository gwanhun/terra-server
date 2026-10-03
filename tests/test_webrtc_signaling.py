"""MqttWebRTCSignaling.publish — 실제 paho MQTTMessageInfo 로 성공/실패 판정 검증.

라우터 테스트는 MqttWebRTCSignaling 을 통째로 stub 하므로 publish() 본체가 실행되지 않는다.
2026-10-03: paho 2.x 의 wait_for_publish() 는 None 을 반환하는데 bool 로 검사해, 발행이
성공해도 항상 Timeout 이 났다(reboot published=false, webrtc ice 502). 그 회귀 방지.
"""

from __future__ import annotations

from typing import Any

import paho.mqtt.client as mqtt
import pytest

from backend import webrtc_signaling
from backend.webrtc_signaling import (
    MqttWebRTCSignaling,
    WebRTCSignalingError,
    WebRTCSignalingTimeout,
)


class _FakeClient:
    """paho Client 대역. publish 는 진짜 MQTTMessageInfo 를 돌려준다."""

    def __init__(self, *, rc: mqtt.MQTTErrorCode, published: bool) -> None:
        self._rc = rc
        self._published = published
        self.calls: list[tuple[str, Any]] = []

    def connect(self, host: str, port: int, keepalive: int) -> None:
        self.calls.append(("connect", (host, port)))

    def loop_start(self) -> None:
        self.calls.append(("loop_start", None))

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> mqtt.MQTTMessageInfo:
        self.calls.append(("publish", (topic, qos, retain)))
        info = mqtt.MQTTMessageInfo(1)
        info.rc = self._rc
        if self._published:
            info._set_as_published()
        return info

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop", None))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", None))


@pytest.fixture
def signaling(monkeypatch: pytest.MonkeyPatch) -> MqttWebRTCSignaling:
    monkeypatch.setenv("MQTT_BRIDGE_USERNAME", "bridge")
    monkeypatch.setenv("MQTT_BRIDGE_PASSWORD", "pw")
    monkeypatch.setattr(webrtc_signaling, "load_dotenv", lambda *_a, **_k: None)
    return MqttWebRTCSignaling()


def _use(monkeypatch: pytest.MonkeyPatch, sig: MqttWebRTCSignaling, client: _FakeClient) -> None:
    monkeypatch.setattr(sig, "_new_client", lambda: client)


def test_publish_success_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, signaling: MqttWebRTCSignaling
) -> None:
    client = _FakeClient(rc=mqtt.MQTT_ERR_SUCCESS, published=True)
    _use(monkeypatch, signaling, client)

    signaling.publish("p4cam-x", {"action": "reboot"})

    assert ("publish", ("esp32/p4cam-x/command", 1, False)) in client.calls
    assert client.calls[-1] == ("disconnect", None)


def test_publish_not_acked_within_timeout_raises_timeout(
    monkeypatch: pytest.MonkeyPatch, signaling: MqttWebRTCSignaling
) -> None:
    client = _FakeClient(rc=mqtt.MQTT_ERR_SUCCESS, published=False)
    _use(monkeypatch, signaling, client)

    with pytest.raises(WebRTCSignalingTimeout):
        signaling.publish("p4cam-x", {"action": "reboot"}, timeout_sec=0.05)
    assert client.calls[-1] == ("disconnect", None)


def test_publish_rc_error_raises_signaling_error(
    monkeypatch: pytest.MonkeyPatch, signaling: MqttWebRTCSignaling
) -> None:
    """paho 가 RuntimeError/ValueError 를 던져도 호출측(except WebRTCSignalingError)이 잡도록 변환."""
    client = _FakeClient(rc=mqtt.MQTT_ERR_NO_CONN, published=False)
    _use(monkeypatch, signaling, client)

    with pytest.raises(WebRTCSignalingError):
        signaling.publish("p4cam-x", {"action": "reboot"}, timeout_sec=0.05)


def test_publish_queue_full_raises_signaling_error(
    monkeypatch: pytest.MonkeyPatch, signaling: MqttWebRTCSignaling
) -> None:
    client = _FakeClient(rc=mqtt.MQTT_ERR_QUEUE_SIZE, published=False)
    _use(monkeypatch, signaling, client)

    with pytest.raises(WebRTCSignalingError):
        signaling.publish("p4cam-x", {"action": "reboot"}, timeout_sec=0.05)
