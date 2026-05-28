# Stage G — 라이브 스트리밍 (⏸️ 보류)

## 개요

사용자가 앱에서 사육장 카메라의 라이브 영상을 본다. 두 단계로 도입:

- **G1**: JPEG snapshot 1초 간격 (의사 라이브, 빠른 PoC)
- **G2**: WebRTC P2P (진짜 라이브, 본격 구현)

ESP32-P4 (메인) 기준. RPi 워커도 동일 인터페이스로 추후 지원.

## In

- Stage F 완료 (카메라 워커 동작 중)
- terra-server REST 라우터 + MQTT 브리지 정상
- (G2 전용) STUN/TURN 서버 결정 (Google STUN 무료 사용 또는 coturn 자체 운영)

## Out

- 다중 시청자 동시 라이브 (1 카메라 = 1 시청자 가정, G3+)
- 음성 양방향 (G3+)
- 녹화 + 라이브 동시 (Stage F 의 모션 캡처와 충돌 시 라이브 우선)
- Pan/Tilt 제어 (별도 명령)

## 완료 조건

### G1 — JPEG Snapshot 라이브 (간단)

#### G1-Backend (terra-server)

- [ ] `backend/routers/cameras.py` 라우터 확장
  - [ ] `POST /cameras/{id}/snapshot-start` (JWT) — 스트리밍 시작 명령 (interval_ms, duration_sec)
  - [ ] `POST /cameras/{id}/snapshot-stop` (JWT)
  - [ ] `POST /cameras/{id}/snapshot` (Camera Token) — 워커가 JPEG 업로드 (R2 PUT)
  - [ ] `GET /cameras/{id}/latest-snapshot.jpg` (JWT) — 앱이 polling
- [ ] `backend/r2_client.py` 에 snapshot 키 유틸 추가
  - [ ] R2 키: `snapshots/{camera_id}/latest.jpg` (덮어쓰기)
  - [ ] lifecycle rule: snapshots/ prefix, 1시간 후 자동 삭제
- [ ] cameras 테이블 `stream_mode` 컬럼 활용 ('snapshot' / NULL)
- [ ] `stream_until` 컬럼으로 자동 종료 시각 관리
- [ ] tests

#### G1-Firmware (ESP32-P4)

- [ ] `main/snapshot_stream.c` 신규
  - [ ] command 수신 → snapshot_stream_task 시작
  - [ ] 1초마다 esp_video 로 JPEG 캡처 (320x240, ~20KB)
  - [ ] terra-api 에 POST (presigned URL 사용)
  - [ ] duration 만료 또는 stop 명령 시 종료

### G2 — WebRTC 라이브 (본격)

#### G2-Backend (terra-server)

- [ ] `backend/routers/webrtc.py` 신규 (시그널링 릴레이)
  - [ ] `POST /cameras/{id}/webrtc/offer` (JWT) — 앱 → 서버 (SDP offer)
  - [ ] `POST /cameras/{id}/webrtc/ice` (JWT) — ICE candidate 추가
  - [ ] `POST /cameras/{id}/webrtc/close` (JWT) — 세션 종료
- [ ] 워커 측 SDP/ICE 는 MQTT 로 전달
  - [ ] command: `{ action: "webrtc_offer", sdp, session_id }`
  - [ ] command: `{ action: "webrtc_ice", candidate, session_id }`
  - [ ] ack: `{ action: "webrtc_answer", sdp, session_id }`
- [ ] STUN/TURN 설정 응답 (`GET /cameras/webrtc/config`)
  - [ ] 1차: Google STUN (`stun:stun.l.google.com:19302`) — 무료, NAT 양쪽 막힌 환경엔 부족
  - [ ] 2차 (필요 시): coturn on Lightsail (~$0~5/월)
- [ ] tests

#### G2-Firmware (ESP32-P4)

- [ ] `main/webrtc_stream.c` 신규
  - [ ] `esp_webrtc` 컴포넌트 사용 (2024년 후반 Espressif 출시)
  - [ ] H.264 stream 을 PeerConnection track 으로 전송
  - [ ] SDP/ICE 처리 (MQTT command 통한 시그널링)
- [ ] 동시 캡처: 모션 녹화와 라이브 stream 의 동일 H.264 출력 공유 (가능 시)

## 설계 메모

### 왜 G1 → G2 단계적?

**G1 장점**:
- 1~2일 구현
- 모든 워커 (RPi/ESP32-P4) 동일 패턴 즉시 가능
- "그냥 사육장 상태 확인" 사용 패턴엔 충분
- 대역폭 작음 (사용자가 잠깐만 봄)

**G2 장점**:
- 진짜 라이브 (지연 <500ms)
- HD/FHD 가능
- 음성 추가 시 그대로 확장

**단점**: G2 는 `esp_webrtc` 자료 적음 (2024 출시). 안정화 시간 필요.

→ G1 으로 사용자 피드백 받고, 진짜 필요하면 G2 진행.

### G1 비용 (대략)

사용자 1명 × 라이브 30분/일:
- JPEG 30분 × 60 × 20KB = ~36MB/일/사용자
- 월 ~1GB → R2 무료 한도 안

사용자 100명:
- 월 100GB → R2 무료 10GB 초과분 ~$1.50/월

### G2 비용

- WebRTC P2P → 서버 트래픽 거의 없음 (~수십 KB/세션, 시그널링만)
- STUN: 무료 (Google)
- TURN: NAT 양쪽 막힌 환경에서만 필요. 본 프로젝트는 ESP32-P4 가 outbound 연결을 카메라에서 시작하니 TURN 필요 빈도 낮음.

### 보안 — 라이브 세션 권한

- 앱이 라이브 시작 요청 시 JWT 검증 → 본인 카메라인지 RLS 체크
- camera_token 으로 워커 인증
- snapshot R2 키는 추측 어려움 (camera_id + UUID)
- WebRTC 세션 ID 는 일회용 UUID, terra-api 가 검증

### 동시성

- 1 카메라 = 1 라이브 세션 (G1: stream_mode, G2: session_id 기준)
- 새 라이브 요청 시 기존 세션 자동 종료
- 추후 N:M 시청 필요하면 미디어 서버 (mediasoup, livekit) 도입 검토

### 모션 캡처와 라이브 동시 운영

- G1 (JPEG snapshot): 영향 없음 (별도 캡처 경로)
- G2 (WebRTC): H.264 인코더 출력을 두 곳 (mp4 + WebRTC track) 으로 분기 가능. esp_h264 가 multi-output 지원 시.

## 학습 노트

### WebRTC 시그널링 = HTTP/MQTT 어디서든 가능
WebRTC 표준은 시그널링 채널 규정 없음 → 본 프로젝트는 terra-api REST + MQTT 조합.

### ESP32-P4 esp_webrtc 컴포넌트
2024년 후반 Espressif 가 공개. PeerConnection, ICE, SRTP 다 지원.
다만 ESP32-P4 의 esp_video → esp_h264 → esp_webrtc 통합 예제는 아직 적음.

### 대체 옵션 (G2 가 어려울 시)
- **MJPEG over HTTP stream** (단순, 대역폭 큼) — 비추
- **HLS via R2** (지연 10초+) — 비추
- **WebSocket binary stream** (커스텀) — 중간 복잡도

## 참고

- [stage-f-camera-ingest.md](stage-f-camera-ingest.md) — 카메라 워커 기본 동작
- [docs/MQTT.md](../docs/MQTT.md) — snapshot/webrtc 토픽
- [docs/API.md](../docs/API.md) — 신규 엔드포인트
- WebRTC samples: https://webrtc.github.io/samples/
- esp_webrtc: (Espressif GitHub 검색)
