"""WebRTC live-stream signaling routes.

These endpoints are used by the web/mobile app.  They validate the user JWT,
check camera ownership, and relay SDP/ICE messages to the ESP32-P4/RPi camera
worker over MQTT.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user_id
from backend.supabase_client import get_supabase_client
from backend.webrtc_relay import get_relay
from backend.webrtc_signaling import (
    MqttWebRTCSignaling,
    WebRTCSignalingError,
    WebRTCSignalingTimeout,
)

logger = logging.getLogger(__name__)

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


class WebRTCCandidatesOut(BaseModel):
    candidates: list[dict[str, Any]] = Field(
        default_factory=list,
        description="펌웨어 측 ICE candidate 목록 (since_index 이후). 빈 배열이면 timeout 까지 아무도 안 옴.",
    )
    next_index: int = Field(
        ...,
        description="다음 poll 호출 시 since_index 로 전달할 값 (서버 buffer 의 현재 크기).",
    )


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


_RTPMAP_RE = re.compile(r'^a=rtpmap:(\d+)\s+([A-Za-z0-9.\-]+)/')
_FMTP_RE = re.compile(r'^a=fmtp:(\d+)\s+(.*)$')
_PT_ATTR_RE = re.compile(r'^a=(?:rtpmap|rtcp-fb|fmtp):(\d+)\b')


def _prune_offer_to_h264(sdp: str) -> str:
    """브라우저 offer 를 esp_peer(카메라)가 파싱 가능한 최소 형태로 깎는다.

    카메라(esp_peer)는 단순한 H.264 offer 만 answer 할 수 있다(네이티브 앱 offer 는 정상 동작).
    Chrome offer 는 그에 비해 과해서 answer 자체를 못 만들고 → ack 무응답 → REST 504 가 된다.
    세 가지를 쳐낸다:

    1. 코덱: VP8/VP9/AV1/H265 + rtx/fec payload type 제거, H.264(pm=1)만 유지(~50개→3개).
    2. mDNS 후보: `a=candidate ...local`(Chrome 호스트 후보)은 호스트명이라 esp_peer 가 못 푼다.
       srflx(공인 IP) 후보는 남겨 ICE 가 되게 한다.
    3. RTP 헤더 확장: `a=extmap`/`extmap-allow-mixed` 제거(recvonly 영상엔 불필요).

    안전성: payload type 은 **삭제만** 하고 renumber 안 한다. 브라우저 원본 offer 가 살아남은 PT 를
    그대로 매핑하므로 카메라가 어떤 PT 로 answer 하든 브라우저가 수용한다. 앱 offer 엔 mDNS/과한
    extmap 이 없어 이 함수가 앱 경로엔 영향을 주지 않는다.
    """
    lines = sdp.replace('\r\n', '\n').split('\n')

    pt_codec: dict[str, str] = {}
    for ln in lines:
        m = _RTPMAP_RE.match(ln)
        if m:
            pt_codec[m.group(1)] = m.group(2).upper()

    keep: set[str] = set()
    for ln in lines:
        m = _FMTP_RE.match(ln)
        if m and pt_codec.get(m.group(1)) == 'H264' and 'packetization-mode=1' in m.group(2):
            keep.add(m.group(1))
    if not keep:  # fmtp 없이도 H.264 가 있으면 전부 유지 (fallback)
        keep = {pt for pt, c in pt_codec.items() if c == 'H264'}
    if not keep:  # H.264 자체가 없으면 손대지 않고 그대로 (원인이 다름)
        return sdp

    out: list[str] = []
    for ln in lines:
        if ln.startswith('m=video '):
            parts = ln.split(' ')
            kept_pts = [p for p in parts[3:] if p in keep]
            out.append(' '.join(parts[:3] + kept_pts))
            continue
        m = _PT_ATTR_RE.match(ln)
        if m and m.group(1) not in keep:
            continue  # 버려진 PT 의 rtpmap/rtcp-fb/fmtp 제거
        # mDNS 호스트 후보(...local) 제거 — esp_peer 는 호스트명 후보를 못 풀고 파서가 깨진다.
        # 앱은 raw IP 후보를 보내서 정상 동작. srflx(공인 IP) 후보는 남겨야 ICE 가 되므로 유지.
        if ln.startswith('a=candidate:') and '.local' in ln:
            continue
        # RTP 헤더 확장(extmap) 정리 — recvonly 영상엔 불필요하고 esp_peer 가 11개를 다 못 씹는다.
        if ln.startswith('a=extmap:') or ln.startswith('a=extmap-allow-mixed'):
            continue
        out.append(ln)

    return '\r\n'.join(out)


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
    # 카메라(esp_peer)의 mbedtls DTLS 서버는 조각난 ClientHello 재조립을 못 함
    # (MBEDTLS_ERR_SSL_FEATURE_UNAVAILABLE). offer 의 setup:actpass → passive 로 바꿔
    # 카메라가 DTLS active(클라이언트)가 되게 강제 → 카메라가 작은 ClientHello 를 보내고
    # 브라우저(DTLS 서버)가 받음. 브라우저는 answer(setup:active)만 보므로 영향 없음.
    offer_sdp = body.sdp.replace('a=setup:actpass', 'a=setup:passive')
    # 코덱 다이어트: H.264 외 payload type 을 쳐내 esp_peer 가 파싱 가능한 크기로 축소.
    pruned = _prune_offer_to_h264(offer_sdp)
    logger.info('webrtc offer SDP %d→%d bytes (camera=%s)', len(offer_sdp), len(pruned), camera['camera_id'])
    offer_sdp = pruned
    command = _command(
        'webrtc_offer',
        session_id,
        body.ttl_sec,
        sdp=offer_sdp,
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


@router.get(
    '/{camera_uuid}/webrtc/candidates',
    response_model=WebRTCCandidatesOut,
    summary='펌웨어 측 ICE candidate 가져오기 (long-poll)',
)
async def get_webrtc_candidates(
    camera_uuid: str,
    session_id: str = Query(..., min_length=1, max_length=128),
    since_index: int = Query(0, ge=0),
    timeout_sec: float = Query(20.0, ge=1.0, le=30.0),
    user_id: str = Depends(get_current_user_id),
) -> WebRTCCandidatesOut:
    """
    펌웨어 esp_webrtc 가 ack 토픽으로 publish 하는 candidate 들을 long-poll 로 전달.

    동작:
    - IceRelay 가 백그라운드에서 `esp32/+/ack` 구독 중. webrtc_ice action 들어오면 buffer 누적.
    - 이 엔드포인트는 since_index 이후의 candidate 들을 반환. 없으면 timeout_sec 까지 대기.
    - 응답의 next_index 를 다음 호출 시 since_index 로 보내면 됨 (간단한 cursor).

    펌웨어가 보낼 메시지 모양:
        topic:   esp32/{camera_id}/ack
        payload: { "action": "webrtc_ice", "session_id": "...", "candidate": { ... } }
    """
    _owned_camera(camera_uuid, user_id)  # 소유권 검증 (404 던짐)
    relay = get_relay()
    candidates = await relay.wait_for_candidates(session_id, since_index, timeout_sec)
    return WebRTCCandidatesOut(
        candidates=candidates,
        next_index=since_index + len(candidates),
    )


@router.post(
    '/{camera_uuid}/webrtc/close',
    response_model=WebRTCCommandOut,
    summary='WebRTC 세션 종료 (best-effort)',
)
def close_webrtc(
    camera_uuid: str,
    body: WebRTCCloseIn,
    user_id: str = Depends(get_current_user_id),
) -> WebRTCCommandOut:
    """
    세션 종료는 idempotent + best-effort. MQTT publish 실패해도 502 안 던짐:
    - 카메라가 이미 끊겼을 수 있고 (네트워크/재부팅), 카메라 측에서 자체 timeout 으로 세션 정리
    - DB 의 stream_mode/until 정리는 어떤 경우든 진행해서 다음 라이브 요청 받을 수 있게
    """
    camera = _owned_camera(camera_uuid, user_id)
    command = _command('webrtc_close', body.session_id, body.ttl_sec)
    publish_ok = True
    try:
        MqttWebRTCSignaling().publish(camera['camera_id'], command)
    except WebRTCSignalingError:
        publish_ok = False
        # close 는 best-effort — 카메라가 못 받아도 서버 측 정리는 진행.
        # 진짜 MQTT 인프라 문제라면 webrtc_signaling 측 ERROR 로그가 누적될 거라 운영 감지 가능.
        logger.warning("webrtc_close publish 실패 (camera=%s session=%s) — 서버 측 정리만 진행",
                       camera.get('camera_id'), body.session_id)

    sb = get_supabase_client()
    sb.table('cameras').update({
        'stream_mode': None,
        'stream_until': None,
    }).eq('id', camera_uuid).eq('owner_id', user_id).execute()

    # ICE candidate buffer 정리 (메모리 누수 방지 + 대기 중 long-poll 깨움)
    get_relay().drop_session(body.session_id)

    return WebRTCCommandOut(ok=publish_ok, session_id=body.session_id)
