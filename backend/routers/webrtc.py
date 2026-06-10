"""WebRTC live-stream signaling routes.

These endpoints are used by the web/mobile app.  They validate the user JWT,
check camera ownership, and relay SDP/ICE messages to the ESP32-P4/RPi camera
worker over MQTT.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.supabase_client import get_supabase_client
from backend.webrtc_signaling import (
    MqttWebRTCSignaling,
    WebRTCSignalingError,
    WebRTCSignalingTimeout,
)

router = APIRouter(prefix='/cameras', tags=['webrtc'])


class WebRTCConfigOut(BaseModel):
    iceServers: list[dict[str, Any]]
    sdpSemantics: str = 'unified-plan'


class WebRTCOfferIn(BaseModel):
    sdp: str = Field(..., min_length=1)
    type: str = Field(default='offer')
    session_id: str | None = Field(None, min_length=1, max_length=128)
    timeout_sec: float = Field(default=15.0, ge=1.0, le=30.0)
    ttl_sec: int = Field(default=30, ge=1, le=120)


class WebRTCAnswerOut(BaseModel):
    session_id: str
    type: str = 'answer'
    sdp: str
    raw: dict[str, Any]


class WebRTCIceIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    candidate: dict[str, Any]
    ttl_sec: int = Field(default=30, ge=1, le=120)


class WebRTCCloseIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    ttl_sec: int = Field(default=10, ge=1, le=60)


class WebRTCCommandOut(BaseModel):
    ok: bool
    session_id: str


def _ice_servers_from_env() -> list[dict[str, Any]]:
    stun_urls = [
        u.strip()
        for u in os.getenv('WEBRTC_STUN_URLS', 'stun:stun.l.google.com:19302').split(',')
        if u.strip()
    ]
    servers: list[dict[str, Any]] = []
    if stun_urls:
        servers.append({'urls': stun_urls})

    turn_urls = [u.strip() for u in os.getenv('WEBRTC_TURN_URLS', '').split(',') if u.strip()]
    turn_user = os.getenv('WEBRTC_TURN_USERNAME', '').strip()
    turn_pass = os.getenv('WEBRTC_TURN_CREDENTIAL', '').strip()
    if turn_urls:
        turn: dict[str, Any] = {'urls': turn_urls}
        if turn_user and turn_pass:
            turn.update({'username': turn_user, 'credential': turn_pass})
        servers.append(turn)
    return servers


def _owned_camera(camera_uuid: str, user_id: str) -> dict[str, Any]:
    sb = get_supabase_client()
    res = (
        sb.table('cameras')
        .select('id, owner_id, camera_id, stream_mode, stream_until')
        .eq('id', camera_uuid)
        .single()
        .execute()
    )
    row = res.data
    if not row or row.get('owner_id') != user_id:
        raise HTTPException(status_code=404, detail='camera not found')
    return row


def _command(action: str, session_id: str, ttl_sec: int, **extra: Any) -> dict[str, Any]:
    payload = {
        'msg_id': str(uuid4()),
        'issued_at': int(time.time()),
        'ttl_sec': ttl_sec,
        'action': action,
        'session_id': session_id,
    }
    payload.update(extra)
    return payload


def _extract_answer_sdp(payload: dict[str, Any]) -> str | None:
    sdp = payload.get('sdp')
    if isinstance(sdp, str) and sdp:
        return sdp
    answer = payload.get('answer')
    if isinstance(answer, dict):
        sdp = answer.get('sdp')
        if isinstance(sdp, str) and sdp:
            return sdp
    return None


@router.get('/webrtc/config', response_model=WebRTCConfigOut, summary='WebRTC STUN/TURN 설정')
def get_webrtc_config(_: str = Depends(get_current_user_id)) -> WebRTCConfigOut:
    return WebRTCConfigOut(iceServers=_ice_servers_from_env())


@router.post(
    '/{camera_uuid}/webrtc/offer',
    response_model=WebRTCAnswerOut,
    summary='앱 SDP offer 를 카메라로 전달하고 answer 반환',
)
def create_webrtc_offer(
    camera_uuid: str,
    body: WebRTCOfferIn,
    user_id: str = Depends(get_current_user_id),
) -> WebRTCAnswerOut:
    camera = _owned_camera(camera_uuid, user_id)
    session_id = body.session_id or str(uuid4())
    command = _command(
        'webrtc_offer',
        session_id,
        body.ttl_sec,
        sdp=body.sdp,
        type=body.type,
    )

    try:
        answer = MqttWebRTCSignaling().request_answer(
            camera['camera_id'],
            command,
            session_id=session_id,
            timeout_sec=body.timeout_sec,
        )
    except WebRTCSignalingTimeout as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
    except WebRTCSignalingError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    sdp = _extract_answer_sdp(answer)
    if not sdp:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='camera answer has no SDP')

    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    sb = get_supabase_client()
    sb.table('cameras').update({
        'stream_mode': 'webrtc',
        'stream_until': until.isoformat(),
    }).eq('id', camera_uuid).eq('owner_id', user_id).execute()

    return WebRTCAnswerOut(session_id=session_id, sdp=sdp, raw=answer)


@router.post(
    '/{camera_uuid}/webrtc/ice',
    response_model=WebRTCCommandOut,
    summary='앱 ICE candidate 를 카메라로 전달',
)
def add_webrtc_ice(
    camera_uuid: str,
    body: WebRTCIceIn,
    user_id: str = Depends(get_current_user_id),
) -> WebRTCCommandOut:
    camera = _owned_camera(camera_uuid, user_id)
    command = _command(
        'webrtc_ice',
        body.session_id,
        body.ttl_sec,
        candidate=body.candidate,
    )
    try:
        MqttWebRTCSignaling().publish(camera['camera_id'], command)
    except WebRTCSignalingError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return WebRTCCommandOut(ok=True, session_id=body.session_id)


@router.post(
    '/{camera_uuid}/webrtc/close',
    response_model=WebRTCCommandOut,
    summary='WebRTC 세션 종료',
)
def close_webrtc(
    camera_uuid: str,
    body: WebRTCCloseIn,
    user_id: str = Depends(get_current_user_id),
) -> WebRTCCommandOut:
    camera = _owned_camera(camera_uuid, user_id)
    command = _command('webrtc_close', body.session_id, body.ttl_sec)
    try:
        MqttWebRTCSignaling().publish(camera['camera_id'], command)
    except WebRTCSignalingError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    sb = get_supabase_client()
    sb.table('cameras').update({
        'stream_mode': None,
        'stream_until': None,
    }).eq('id', camera_uuid).eq('owner_id', user_id).execute()
    return WebRTCCommandOut(ok=True, session_id=body.session_id)
