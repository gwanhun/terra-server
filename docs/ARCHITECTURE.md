# 아키텍처

## 한 줄 요약

ESP32-S3(센서/제어) + ESP32-P4(카메라/영상/라이브) → terra-server (Lightsail) → Supabase + R2 ← 앱

## 시스템 맵

```
┌─────────────────────────────────┐                              ┌──────────────────────────┐
│  사육장 (enclosure)             │                              │  AWS Lightsail $3.50     │
│                                 │                              │  Ubuntu 22.04, 서울       │
│  ┌────────────────┐             │   outbound TCP               │                          │
│  │  ESP32-S3      │ ─────────────── mqtts://...:8883 ────────>│  ┌────────────────┐     │
│  │  (센서/제어)   │             │                              │  │  Mosquitto 2.x │     │
│  │  - DHT22 x2    │             │                              │  │  (TLS, 8883)   │     │
│  │  - 펌프/팬/히터│             │                              │  └───────┬────────┘     │
│  │  - LED 컨트롤  │             │                              │          │              │
│  └────────────────┘             │                              │  ┌───────▼────────┐     │
│                                 │                              │  │  terra-bridge  │     │
│  ┌────────────────────┐         │   outbound TCP               │  │  (systemd)     │     │
│  │  ESP32-P4          │ ───────── mqtts://...:8883 ──────────>│  └───────┬────────┘     │
│  │  + MIPI Camera     │         │   (motion_event 알림)        │          │              │
│  │  (HW H.264 + WiFi6)│         │                              │  ┌───────▼────────┐     │
│  │  - esp_video       │ ───── HTTPS POST presigned URL ──────>│  │  FastAPI       │     │
│  │  - esp_h264        │         │   (terra-api 가 URL 발급)    │  │  (uvicorn)     │     │
│  │  - esp_webrtc      │         │                              │  └────────┬───────┘     │
│  └────────┬───────────┘         │                              └───────────┼──────────────┘
│           │                     │                                          │
└───────────┼─────────────────────┘                                          │ HTTPS
            │                                                                ▼
            │ ┌─────────────────────────────────────────┐  ┌──────────────────────────┐
            │ │ HTTPS PUT (mp4 ~500KB~1.5MB)             │  │  Supabase                │
            ├─┤                                          │  │  - Auth (JWT)            │
            │ │ WebRTC P2P (라이브 스트림, Stage G2)      │  │  - Postgres + RLS        │
            │ │  └→ STUN/TURN (필요 시)                  │  │  - Realtime (WS push)    │
            │ └─────────────────────────────────────────┘  └────────┬─────────────────┘
            ▼                                                       │
   ┌────────────────────┐ <───── 메타만 INSERT (r2_key) ──────────│
   │  Cloudflare R2     │                                          │
   │  (영상 파일 저장)   │                                          │ HTTPS + Realtime
   └─────────┬──────────┘                                          ▼
             │                                          ┌────────────────────────┐
             │ presigned GET URL (1h TTL)              │  앱 (모바일/웹)        │
             └───── 앱이 직접 GET (VOD 재생) ─────────│  - 실시간 센서값       │
                                                      │  - 모션 클립 재생      │
                                                      │  - 라이브 뷰 (JPEG/WebRTC) │
                                                      │  - 액추에이터 제어      │
                                                      └────────────────────────┘
```

## 디바이스 분담 이유

| 보드 | 종류 | 강점 | 본 프로젝트 역할 |
|------|------|------|----------------|
| **ESP32-S3** | MCU | 실시간성, 저전력, 24/7 무중단, 안전 액추에이터 제어 | 센서 읽기, 액추에이터 ON/OFF, 안전 latch |
| **ESP32-P4** (+ ESP32-C6 WiFi 코프로세서) | MCU + 영상 SoC | HW H.264 인코더 내장, MIPI-CSI 카메라, BLE 5.0, 저전력 | 영상 캡처 + H.264 인코딩 + R2 업로드 + 라이브 스트리밍 |

→ ESP32 생태계 통일 (ESP-IDF + FreeRTOS + NimBLE). 본 프로젝트 펌웨어 자산 재활용 가능.

> **대안 워커**: Raspberry Pi Zero 2 W (Linux/Python 기반).
> 사용 시나리오는 [cloud_integration.md](../../esp32/NimBLE_Connection/docs/cloud_integration.md) 0장 참조.
> 백엔드 인터페이스(R2 PUT + REST presigned URL)는 동일하므로 사육장별로 선택 가능.

## 프로세스 구성 (Lightsail 내부)

| 프로세스 | 진입점 | 역할 |
|---------|--------|------|
| `terra-api` | `backend.main:app` (uvicorn) | REST API (페어링, CRUD, presigned URL 발급, WebRTC 시그널링) |
| `terra-bridge` | `backend.mqtt_bridge_main:run` | MQTT ↔ Supabase 양방향 (telemetry/ack/alert/command/motion_event) |
| `mosquitto` | systemd unit (apt 패키지) | MQTT 브로커 (TLS 8883) |

→ 모두 같은 Lightsail VPS 에서 systemd 로 가동.

## 도메인 모델 — `enclosure` 가 최상위

```
auth.users (Supabase)
   │
   │ owner_id
   ▼
┌────────────┐
│ enclosures │ (= 사육장)
└────┬───────┘
     │
     ├──► devices    (ESP32-S3, 1:N)
     │     ├──► device_settings (1:1)
     │     ├──► telemetry / telemetry_1m
     │     ├──► commands
     │     └──► alerts
     │
     └──► cameras    (ESP32-P4 / RPi 워커, 1:N)
           └──► motion_clips (R2 메타, H.264 mp4)
```

- 한 사용자가 여러 enclosure 보유 가능
- 한 enclosure 는 ESP32-S3 디바이스 N개 + 카메라 N개 (보통 각 1개)
- **enclosure 없이 단독 디바이스/카메라도 허용**

## 데이터 흐름

### 1. 디바이스/카메라 페어링 (둘 다 BLE 기반)

#### ESP32-S3 페어링

```
ESP32-S3 → BLE 광고 → 앱 BLE 스캔
앱 → BLE write: { ssid, password, jwt, enclosure_id?, name }
ESP32-S3 → WiFi 연결 → HTTPS POST /devices/pair (jwt 헤더)
terra-api → JWT 검증 → 토큰 발급 → DB INSERT → 응답 { device_id, mqtt_token }
ESP32-S3 → NVS 저장 → MQTT connect
```

#### ESP32-P4 페어링 (동일 패턴)

```
ESP32-P4 → BLE 5.0 광고 (ESP32-C6 통한 BT) → 앱 스캔
앱 → BLE write: { ssid, password, jwt, enclosure_id?, name }
ESP32-P4 → WiFi 연결 → HTTPS POST /cameras/pair (jwt 헤더)
terra-api → JWT 검증 → 토큰 발급 → DB INSERT → 응답 { camera_id, mqtt_token }
ESP32-P4 → NVS 저장 → MQTT connect + 워커 시작
```

→ ESP32-S3 펌웨어의 NimBLE 페어링 코드를 ESP32-P4에서도 재사용 (BLE 5.0 도 NimBLE 호환).

### 2. 센서 Telemetry (ESP32-S3, 3초 주기)

```
ESP32-S3 → publish esp32/{device_id}/telemetry (QoS 0)
terra-bridge → Supabase telemetry INSERT + devices.last_seen_at 갱신
앱 ← Supabase Realtime push
```

### 3. 모션 영상 업로드 (ESP32-P4 워커, Stage F)

```
ESP32-P4 (펌웨어, FreeRTOS 태스크)
  ├─ motion_detect_task: esp_video 프레임 → 차분 계산 → 모션 감지
  ├─ capture_task: 10초 H.264 mp4 (esp_h264 + esp_mp4 컨테이너) → PSRAM 버퍼
  └─ upload_task:
       ├─ POST /cameras/{camera_id}/clips/upload-url → presigned PUT URL 수신
       ├─ PUT <url> with mp4 (~500KB~1.5MB, chunked)
       └─ POST /cameras/{camera_id}/clips → 메타 INSERT

ESP32-P4 → publish esp32/{camera_id}/motion_event (QoS 1)
terra-bridge → motion_event 로깅
앱 ← Supabase Realtime push (motion_clips 신규 row)
앱 → GET /clips/{id}/url → 앱이 R2 직접 GET
```

### 4. 라이브 스트리밍 (Stage G — 신규)

#### Stage G1 — JPEG snapshot (간단)

```
앱 "라이브 보기" 진입
  → POST /cameras/{id}/snapshot-start { interval_ms: 1000, duration_sec: 300 }
terra-bridge → MQTT command: { action: "snapshot_stream", interval_ms: 1000 }
ESP32-P4 → 1초마다 320x240 JPEG → POST /cameras/{id}/snapshot (Camera Token)
terra-api → R2 PUT (key: snapshots/{camera_id}/latest.jpg, lifecycle 1h)
앱 → 1초마다 GET /cameras/{id}/latest-snapshot.jpg (presigned GET URL)

앱 닫음 또는 duration 만료
  → POST /cameras/{id}/snapshot-stop
terra-bridge → MQTT command: { action: "snapshot_stop" }
```

#### Stage G2 — WebRTC (본격 라이브)

```
앱 "HD 라이브" 진입
  → POST /cameras/{id}/webrtc/offer { sdp: ... }
terra-api → MQTT command: { action: "webrtc_offer", sdp: ..., session_id }
ESP32-P4 (esp_webrtc) → SDP answer 생성 → publish ack { sdp: ... }
terra-api → 앱에 응답: { sdp: answer, session_id }

ICE 후보 교환 (terra-api 가 시그널링 릴레이)
WebRTC 연결 수립 (P2P 또는 TURN 경유)

ESP32-P4 → WebRTC track 으로 H.264 stream 직접 송출 → 앱 재생
terra-server 는 시그널링만 (트래픽 없음)

앱 종료
  → POST /cameras/{id}/webrtc/close
terra-api → MQTT command: { action: "webrtc_close", session_id }
```

### 5. 명령 (앱 → ESP32-S3) — 기존 동일

### 6. 알림

- ESP32-S3: heater_latched, sensor_fault 등
- ESP32-P4: sd_full, r2_upload_failed, webrtc_failed 등

## 보안 계층

### 채널별 보안

| 계층 | 메커니즘 | TLS 인증서 출처 |
|------|---------|----------------|
| ESP32-S3 / ESP32-P4 ↔ Mosquitto | TLS 1.2+, per-device username/password | Let's Encrypt (`mqtt.example.com`, certbot) |
| terra-bridge ↔ Mosquitto | TLS + 전용 username (모든 토픽) | 위 동일 |
| ESP32-P4 ↔ R2 | TLS + presigned PUT URL (TTL 5분, terra-api 발급) | R2 (Cloudflare 자체) |
| ESP32-P4 ↔ terra-api | TLS + Bearer token (camera_token, NVS 평문) | Let's Encrypt (`api.example.com`, Caddy) |
| 앱 ↔ terra-api | HTTPS + Bearer JWT | Let's Encrypt (`api.example.com`, Caddy) |
| 앱 ↔ Supabase | HTTPS + anon key + JWT + RLS | Supabase 자체 |
| 앱 ↔ R2 (영상 재생) | TLS + presigned GET URL (TTL 1시간) | R2 (Cloudflare 자체) |
| 앱 ↔ ESP32-P4 (WebRTC) | DTLS + SRTP (WebRTC 표준) | WebRTC 자체 fingerprint |
| 영상 파일 자체 | R2 버킷 비공개 (모든 접근은 presigned URL 통과) | - |
| 디바이스/카메라 토큰 | bcrypt 해시 DB 저장, 평문은 페어링 응답에만 1회 | - |

### Lightsail 인스턴스의 인증서 구성 (요약)

```
[mqtt.example.com]  ──certbot --standalone──>  /etc/letsencrypt/live/...
                                                    ↓ (직접 파일 참조)
                                                Mosquitto :8883

[api.example.com]   ──Caddy 자동 ACME──>      /var/lib/caddy/...
                                                    ↓
                                                Caddy :443 → reverse_proxy 127.0.0.1:8000
                                                                              ↓
                                                                          uvicorn (terra-api)
```

- **자동 갱신**: certbot.timer (Mosquitto 측), Caddy 내부 스케줄러 (API 측). 둘 다 사람 개입 없음.
- **갱신 후 후속**: certbot post-hook 이 mosquitto 재시작 / Caddy 는 hot reload (재시작 불필요).
- **운영 상세**: [docs/DEPLOYMENT.md](DEPLOYMENT.md#tls-인증서-운영-현재-구성-정리) "TLS 인증서 운영" 섹션.

## 비용 추정 (디바이스 1대 + 카메라 1대 기준, 월)

| 항목 | 사용량 | 비용 |
|------|-------|------|
| Lightsail VPS | $3.50/월 | $3.50 |
| Supabase Free | 500MB DB / 1GB egress | $0 |
| Cloudflare R2 (VOD) | 30GB 저장 (1000 클립/일 × 1MB × 30일) | $0.30 |
| Cloudflare R2 (snapshot) | 무시할 정도 (latest.jpg lifecycle 1h) | $0 |
| TURN 서버 (WebRTC, 옵션) | coturn on Lightsail 같이 / 또는 무료 STUN 만 | $0 |
| 도메인 | 연 등록 | ~$1/월 |
| **합계** | | **~$4.80/월** |

R2 무료 한도:
- 저장: 10GB / Class A: 1M / Class B: 10M / Egress: **무제한 무료**

WebRTC P2P 시 트래픽 비용 거의 없음 (서버는 시그널링만, ~수십 byte/세션).

## 확장 시 고려

- **카메라 영상 누적 압박** — R2 lifecycle rule 30일 후 자동 삭제 + DB row 동기 삭제 (Stage F2)
- **TURN 서버 필요 시** — coturn 을 같은 Lightsail 에 같이 띄움 (RAM ~50MB 추가)
- **앱 영상 재생** — mp4/H.264 는 iOS/Android 기본 호환 완벽
- **디바이스 100+** — Lightsail $5 (1GB) 업그레이드
- **AI 행동 분류** — 별도 워커 추가 가능 (현재 범위 외)
