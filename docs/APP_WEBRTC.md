# 카메라 라이브 스트리밍 적용 가이드 (WebRTC)

> 모바일/웹 앱이 사육장 카메라(ESP32-P4) 라이브 영상을 보기 위한 단일 진실 소스.
> 참조 구현: [web/index.html](../web/index.html) 의 `openLive` / `startCandidatePolling` / `closeLive`.
> 서버 라우터: [backend/routers/webrtc.py](../backend/routers/webrtc.py).
> 시그널링 동기: [backend/webrtc_relay.py](../backend/webrtc_relay.py) (펌웨어→웹 ICE 릴레이).

## 0. 한 줄 요약

**앱은 RTCPeerConnection 만 만들면 된다.** 시그널링(SDP/ICE)은 모두 terra-server REST 가 MQTT 로 펌웨어에 중계. 영상은 R2/Supabase 안 거치고 **카메라 ↔ 앱 P2P** 로 직접 흐름.

## 1. 전제

- 사용자 JWT 확보 ([APP_INTEGRATION.md §1](APP_INTEGRATION.md#1-인증-supabase-auth))
- 카메라 페어링 완료 (`POST /cameras/pair`) — `camera_uuid` 보유
- 카메라가 온라인 (`GET /cameras` 의 `is_online: true` 확인 권장)
- 펌웨어가 `webrtc_offer` action 처리 + ICE candidate 를 ack 토픽에 publish ([FIRMWARE_INTEGRATION.md](FIRMWARE_INTEGRATION.md) 참조)

## 2. 의존성 (플랫폼별)

| 플랫폼 | 패키지 |
|--------|--------|
| Flutter | `flutter_webrtc` ^0.11.0 |
| iOS native | [WebRTC.xcframework](https://webrtc.googlesource.com/src/) — SPM 또는 수동 |
| Android native | `io.github.webrtc-sdk:android:114.5735.10` |
| React Native | `react-native-webrtc` ^124 |
| Web | 브라우저 내장 `RTCPeerConnection` (Chrome/Edge/Safari/Firefox 모두 지원) |

표준 W3C WebRTC API 라 의존성 SDK 만 다르고 호출 흐름은 동일.

## 3. 시퀀스 (참조: [web/index.html:917-1010](../web/index.html))

```
앱                          terra-server                     펌웨어
 │                                │                              │
 │  GET /cameras/webrtc/config    │                              │
 │ ─────────────────────────────► │                              │
 │ ◄──── {iceServers, sdpSemantics} ─                            │
 │                                │                              │
 │  RTCPeerConnection 생성        │                              │
 │  addTransceiver(video,recvonly)│                              │
 │  await waitIceGathering(2s)    │   (offer SDP 안에 ICE 후보 포함)
 │  createOffer / setLocal        │                              │
 │                                │                              │
 │  POST /webrtc/offer            │                              │
 │  body: {sdp, type:"offer"}     │   MQTT publish               │
 │ ─────────────────────────────► │ ───────────────────────────► │ esp32/{cam_id}/command
 │                                │                              │   { action:"webrtc_offer",
 │                                │                              │     session_id, sdp }
 │                                │                              │
 │                                │  (펌웨어가 answer 생성)     │
 │                                │ ◄─── MQTT publish ─────────  │ esp32/{cam_id}/ack
 │                                │                              │   { action:"webrtc_answer",
 │                                │                              │     session_id, sdp }
 │ ◄── { session_id, sdp, ... } ──                               │
 │                                │                              │
 │  setRemoteDescription(answer)  │                              │
 │                                │                              │
 │ ╔═══ 양방향 ICE trickle ═════════════════════════════════════╗ │
 │ ║                              │                            ║ │
 │ ║ pc.onicecandidate            │                            ║ │
 │ ║   POST /webrtc/ice ──────────► MQTT command ──────────────► (펌웨어 측 add)
 │ ║                              │                            ║ │
 │ ║ GET /webrtc/candidates       │ ◄─── ack {webrtc_ice} ──── (펌웨어 candidate)
 │ ║   (long-poll, 20s)           │                            ║ │
 │ ║ ◄── {candidates, next_index} ─                            ║ │
 │ ║  pc.addIceCandidate(each)    │                            ║ │
 │ ║                              │                            ║ │
 │ ║  ↻ next_index 로 다시 poll  │                            ║ │
 │ ╚════════════════════════════════════════════════════════════╝ │
 │                                │                              │
 │  ICE pair 성공                 │                              │
 │  pc.connectionState=connected  │                              │
 │  pc.ontrack ← video track ─────────────────── P2P RTP ──────► (카메라에서 직접)
 │                                │                              │
 │  ── 시청 ──                    │                              │
 │                                │                              │
 │  사용자가 닫기                 │                              │
 │  POST /webrtc/close            │   MQTT publish              │
 │ ─────────────────────────────► │ ───────────────────────────► │ { action:"webrtc_close" }
 │  pc.close()                    │   buffer drop_session()      │
 │  candidate polling 종료        │   cameras.stream_mode=NULL   │
```

## 4. REST API 4개

기본 URL: `https://api.terra-server.uk`
모든 호출에 `Authorization: Bearer <user_jwt>` 필요. 카메라 토큰 X.

### 4.1 STUN/TURN 설정

```
GET /cameras/webrtc/config
→ 200
{
  "iceServers": [
    { "urls": ["stun:stun.l.google.com:19302"] },
    { "urls": ["turn:..."], "username": "...", "credential": "..." }   // 옵션
  ],
  "sdpSemantics": "unified-plan"
}
```

응답을 그대로 `RTCConfiguration` 에 전달. TURN 은 서버 `.env` 의 `WEBRTC_TURN_*` 가 비어있으면 응답에 없음.

### 4.2 Offer 전송 (동기 — answer 동기 대기)

```
POST /cameras/{camera_uuid}/webrtc/offer
body:
{
  "sdp": "<SDP 텍스트>",
  "type": "offer",
  "session_id": "<옵션, 없으면 서버가 UUID 발급>",
  "timeout_sec": 15.0,    // 펌웨어 응답 대기 시간 (1~30s)
  "ttl_sec": 30           // 펌웨어 측 명령 TTL
}

→ 200 (성공)
{
  "session_id": "<이후 ice/close 호출 시 같은 값으로>",
  "type": "answer",
  "sdp": "<펌웨어가 만든 answer SDP>",
  "raw": { ... }   // 펌웨어 원본 ack payload 그대로
}

→ 504 (timeout) — 펌웨어가 timeout_sec 안에 답 안 함
→ 502 (bad gateway) — MQTT 자체 실패
```

**중요**: 응답의 `session_id` 를 모듈 변수에 저장. 이후 ICE / close 호출에 모두 같이 보냄.

### 4.3 ICE candidate 양방향

#### 앱 → 펌웨어 (trickle, fire-and-forget)
```
POST /cameras/{camera_uuid}/webrtc/ice
body:
{
  "session_id": "<offer 응답의 session_id>",
  "candidate": { ...RTCIceCandidateInit... }    // candidate, sdpMid, sdpMLineIndex
}

→ 200 { "ok": true, "session_id": "..." }
```

`pc.onicecandidate` 콜백마다 호출. `candidate === null` (gathering 완료) 이벤트는 보내지 않음.

#### 펌웨어 → 앱 (long-poll)
```
GET /cameras/{camera_uuid}/webrtc/candidates?session_id=...&since_index=N&timeout_sec=20

→ 200
{
  "candidates": [ {...}, {...}, ... ],   // since_index 이후의 후보들
  "next_index": <다음 호출의 since_index>
}
```

answer 받자마자 `since_index=0` 부터 폴링 시작. 응답 받으면 각 candidate 를 `pc.addIceCandidate()` 하고 `next_index` 로 즉시 재호출. PeerConnection 이 `connected` / `failed` / `closed` 면 폴링 중단.

빈 배열로 timeout 되면 즉시 다시 호출 (긴 polling chain).

### 4.4 세션 종료 (best-effort)

```
POST /cameras/{camera_uuid}/webrtc/close
body: { "session_id": "...", "ttl_sec": 10 }

→ 200 { "ok": true|false, "session_id": "..." }   // ok=false 면 MQTT publish 실패 (DB 정리는 됨)
```

MQTT publish 실패해도 200. 클라이언트는 응답 모양 안 봐도 됨. 종료 후 서버는 `cameras.stream_mode=NULL`, candidate buffer drop.

## 5. PeerConnection 설정 가이드

### 5.1 핵심 옵션

```dart
// Flutter (flutter_webrtc) 예시
final pc = await createPeerConnection({
  'iceServers': cfg['iceServers'],
  'sdpSemantics': 'unified-plan',
});

// recvonly video transceiver — 앱은 받기만 (마이크 권한 안 필요)
await pc.addTransceiver(
  kind: RTCRtpMediaType.RTCRtpMediaTypeVideo,
  init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
);

pc.onTrack = (RTCTrackEvent ev) {
  // ev.streams[0] 을 RTCVideoRenderer 에 연결
};

pc.onIceCandidate = (RTCIceCandidate c) async {
  if (c.candidate == null) return;
  await http.post(
    Uri.parse('$baseUrl/cameras/$cameraUuid/webrtc/ice'),
    headers: {'Authorization': 'Bearer $jwt', 'Content-Type': 'application/json'},
    body: jsonEncode({
      'session_id': sessionId,
      'candidate': {
        'candidate': c.candidate,
        'sdpMid': c.sdpMid,
        'sdpMLineIndex': c.sdpMLineIndex,
      },
    }),
  );
};
```

### 5.2 Offer 만들기 + ICE gathering 대기

```dart
final offer = await pc.createOffer({'offerToReceiveVideo': true});
await pc.setLocalDescription(offer);

// 펌웨어 측 trickle 처리 불안정성 회피 — gathering 짧게 기다려서
// offer SDP 안에 ICE 후보를 같이 박아 보냄 (non-trickle 효과)
await _waitIceGatheringComplete(pc, timeoutMs: 2000);

final res = await http.post(
  Uri.parse('$baseUrl/cameras/$cameraUuid/webrtc/offer'),
  headers: {...},
  body: jsonEncode({
    'sdp': (await pc.getLocalDescription())!.sdp,
    'type': 'offer',
  }),
);
final ans = jsonDecode(res.body);
sessionId = ans['session_id'];
await pc.setRemoteDescription(RTCSessionDescription(ans['sdp'], 'answer'));
```

### 5.3 펌웨어 측 candidate 폴링 시작

```dart
int sinceIndex = 0;
bool active = true;

Future<void> pollLoop() async {
  while (active && pc.connectionState != RTCPeerConnectionState.RTCPeerConnectionStateClosed
                && pc.connectionState != RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
    final res = await http.get(
      Uri.parse('$baseUrl/cameras/$cameraUuid/webrtc/candidates'
                '?session_id=$sessionId&since_index=$sinceIndex&timeout_sec=20'),
      headers: {'Authorization': 'Bearer $jwt'},
    );
    if (res.statusCode != 200) {
      await Future.delayed(Duration(seconds: 1));
      continue;
    }
    final body = jsonDecode(res.body);
    for (final c in (body['candidates'] as List)) {
      try {
        await pc.addCandidate(RTCIceCandidate(
          c['candidate'], c['sdpMid'], c['sdpMLineIndex'],
        ));
      } catch (e) {
        // 잘못된 후보 1개로 흐름 깨면 안 됨 — 로그만
      }
    }
    sinceIndex = body['next_index'];
  }
}
unawaited(pollLoop());  // 백그라운드 시작
```

### 5.4 종료

```dart
active = false;                              // 폴링 멈춤
await pc.close();
await http.post(
  Uri.parse('$baseUrl/cameras/$cameraUuid/webrtc/close'),
  headers: {...},
  body: jsonEncode({'session_id': sessionId}),
);
```

## 6. UI 가이드

| 항목 | 값 |
|------|-----|
| 비디오 비율 | 펌웨어 현재 1280x960 = **4:3**. `RTCVideoRenderer` 의 aspect-ratio CSS/위젯 속성에 4/3. |
| 동적 비율 | `RTCVideoRenderer` 의 `onResize` (또는 ontrack 의 first frame) 후 실제 해상도로 갱신 권장. 향후 펌웨어 해상도 바뀌어도 자동. |
| 상태 표시 | "STUN 가져오는 중" → "offer 전송 중" → "answer 적용" → "ICE 연결 중" → "영상 수신 중" 단계 노출. 디버깅에 큰 도움. |
| 닫기 버튼 | 모달/페이지 종료 시 반드시 `close` 호출 + `pc.close()` + 폴링 active=false. |

## 7. 에러 / 상태 코드

| 상황 | 코드 | 대응 |
|------|-----|-----|
| 카메라가 webrtc_offer 처리 안 함 | 504 (`/webrtc/offer`) | "카메라 응답 없음" 안내. 카메라 재부팅 권장. |
| MQTT 인프라 문제 | 502 (`/webrtc/offer`) | 잠시 후 재시도. 빈번하면 서버 로그 확인. |
| 권한 없는 카메라 UUID | 404 | 본인 소유 카메라만 가능. |
| 미인증 | 401 | JWT 갱신 후 재시도. |
| close 시 MQTT 실패 | 200 + `ok:false` | 무시. DB 측 정리는 됨. |
| ICE 페어 실패 | `pc.connectionState=failed` | 5.5 트러블슈팅. |

## 8. 트러블슈팅

### "answer 받았는데 영상이 안 옴 (connecting → failed)"
- **원인 1**: 앱 → 펌웨어 ICE candidate 전달 실패. 네트워크 / `/webrtc/ice` 응답 확인.
- **원인 2**: 펌웨어 → 앱 candidate 폴링 시작 안 함. answer 받자마자 `pollLoop()` 호출했는지.
- **원인 3**: NAT 통과 실패. mobile cellular ↔ home Wi-Fi 같은 토폴로지면 TURN 필수.

### "답 SDP 가 mDNS `.local` 만 있어 페어 못 만듦"
모바일에서 mDNS 후보를 못 해석하는 경우. 펌웨어가 raw IP host candidate 만 보내도록 esp_webrtc 설정 확인 또는 STUN 후보 우선.

### "504 가 자주 뜸"
- 펌웨어가 정말 MQTT 에 붙어있는지 (`GET /cameras` 의 `is_online`).
- `timeout_sec` 늘려보기 (최대 30).
- 펌웨어 측 `webrtc_offer` action 핸들러가 매번 정상 응답하는지 (메모리 부족 등 일시적 실패 가능).

### "라이브 닫고 다시 열면 ICE 안 됨"
이전 세션의 `session_id` 가 살아남았을 가능성. close 호출이 누락되면 펌웨어 측 esp_webrtc session 이 lingering. 닫기 흐름에서 반드시 `close` 호출.

## 9. 보안 / 비용

- **WebRTC 자체는 DTLS-SRTP** 로 카메라 ↔ 앱 종단 암호화. terra-server 는 미디어를 절대 보지 않음 (시그널링만).
- TURN 사용 시 미디어가 TURN 서버를 거침 → coturn 자체 운영 권장 (Cloudflare TURN / Twilio 등 외부 서비스는 비용 큼).
- STUN-only 라면 비용 0. 80% 시나리오는 STUN 만으로 OK.

## 10. 관련 문서

- [APP_INTEGRATION.md](APP_INTEGRATION.md) — 전체 앱 통합 (인증/CRUD/Realtime)
- [API.md](API.md) — REST 전체 명세
- [FIRMWARE_INTEGRATION.md](FIRMWARE_INTEGRATION.md) — 펌웨어 측 의무 (webrtc_offer / webrtc_ice / webrtc_close 처리)
- [ENV.md §WebRTC](ENV.md) — 서버 측 STUN/TURN 환경변수
