# Terra — 프로젝트 개요 (한 페이지)

> 5분 안에 파악할 수 있도록 정리한 요약. 상세는 [docs/](docs/) 참고.

## 한 문장

**파충류/양서류 사육장의 센서·제어(ESP32-S3)와 영상·라이브(ESP32-P4)를 하나의 클라우드 백엔드에서 관리하고, 모바일/웹 앱이 실시간으로 모니터링/제어/라이브 시청한다.**

## 시스템 다이어그램

```
[사육장]                              [Lightsail VPS $3.50]            [Supabase]
                                                                      
  ESP32-S3 ──MQTTS(outbound)──┐                                        ├─ Auth (JWT)
  (센서/제어, MCU)             │       ┌──────────────┐                ├─ Postgres
                              ├──────►│  Mosquitto   │──┐             │  + RLS
  ESP32-P4 ──MQTTS────────────┘       │  (TLS 8883)  │  │             ├─ Realtime
  + MIPI Camera                        └──────────────┘  │             │  (WebSocket)
  (영상/라이브, HW H.264)                                ▼             │
       │                                       ┌────────────────┐    │
       │ HTTPS PUT (H.264 mp4, ~1MB)           │  terra-bridge  │────►│
       │ + WebRTC P2P (라이브, Stage G2)        │  + terra-api   │     │
       ▼                                       │  (FastAPI)     │     │
  Cloudflare R2                                 └────────────────┘     │
  (모션 클립, ~3GB/월)                                                  │
       │                                                               │
       └───presigned GET URL ───────────────────────┬──────────────────┘
                                                    │
                                                    ▼
                                            [모바일/웹 앱]
                                            - 실시간 센서값
                                            - 모션 영상 재생
                                            - 라이브 뷰 (JPEG / WebRTC)
                                            - 액추에이터 제어
                                            - 알림 수신
```

> **카메라 워커 대안**: Raspberry Pi Zero 2 W + Camera v3 (Linux/Python). 인터페이스 동일. cloud_integration.md 0장 참조.

## 핵심 결정 사항

| 항목 | 선택 | 이유 |
|------|------|------|
| **호스팅** | AWS Lightsail VPS ($3.50/월, 서울) | 공유기 포트포워딩 회피 + 비용 최저 |
| **통신 프로토콜** | MQTT QoS 1 + TLS (outbound) | 디바이스가 먼저 연결 → NAT/공유기 설정 불필요 |
| **DB / 인증** | Supabase (Postgres + Auth + RLS + Realtime) | BaaS, 앱이 직접 안전 접근 가능 |
| **영상 저장소** | Cloudflare R2 (S3 호환) | egress 무료, 10GB 무료 한도 |
| **API 서버** | Python 3.12 + FastAPI + uvicorn | 가벼움, petcam-lab 패턴 재사용 |
| **하드웨어 분리** | ESP32-S3 (센서) + ESP32-P4 (영상) | 둘 다 MCU/ESP-IDF, 코드 자산 공유 + 저전력 + 양산 적합 |
| **영상 인코딩** | H.264 (HW 가속, ESP32-P4 esp_h264) | iOS/Android 호환 + 파일 크기 ESP32-CAM(MJPEG) 대비 5~10배 작음 |
| **영상 분석** | 안 함 (메타만 저장) | terra-server 부담 ↓, ESP32-P4 자체 모션 감지 |
| **라이브 스트리밍** | G1: JPEG snapshot 1초 → G2: WebRTC P2P | 단계적 도입. G1 1~2일, G2 본격 |

## 비용 (월)

### 서버 측 (사용자 수 무관)

| 항목 | 사용량 (1 사육장 기준) | 비용 |
|------|----------------------|------|
| Lightsail VPS | 512MB / 2 vCPU / 20GB | $3.50 |
| Supabase | Free tier (500MB DB, 1GB egress) | $0 |
| Cloudflare R2 | 30GB 저장 (1000 클립/일 × 1MB × 30일) | $0.30 |
| 도메인 | 연 $12 가정 | ~$1 |
| **합계** | | **~$4.80/월** |

### 디바이스 1대 측 (사용자 부담)

| 항목 | 가격 |
|------|------|
| ESP32-S3 + 센서/액추에이터 + 케이스 | ~$30~50 |
| ESP32-P4 (FireBeetle 2) + MIPI 카메라 + 어댑터 | ~$50~60 |
| **사육장 1대 합계** | **~$80~110** |

> RPi 워커 선택 시: ~$60 (보드+카메라+SD)

- 디바이스 100대 + 카메라 100대 확장 시: 같은 인스턴스로 운영 가능 (RAM 여유 시)
- 영상 누적 30일 보관 후 R2 lifecycle 로 자동 삭제

## 개발 단계 (Stages)

| Stage | 내용 | 예상 |
|-------|------|------|
| **A** | MQTT 브리지 telemetry → Supabase 저장 | 1주 |
| **B** | BLE + JWT 페어링 + MQTT 토큰 발급 | 1주 |
| **C** | 명령 디스패치 (앱 → ESP32) | 1주 |
| **D** | 알림 (alerts + FCM 푸시) | 1~2주 |
| **E** | 시계열 다운샘플 (pg_cron) | 3일 |
| **F** | ESP32-P4 카메라 워커 + 모션 영상 인제스트 | 3~4주 (ESP-IDF 펌웨어 + 백엔드 라우터) |
| **G** | 라이브 스트리밍 (G1 JPEG → G2 WebRTC) | G1: 1주 / G2: 2~3주 |

→ 백엔드 단독: **~5~6주**, 펌웨어/워커 포함 전체: **~10~13주**

> G1만 도입하면 ~9주, G2까지 본격 도입하면 ~13주

## 보안 모델 (요약)

- 디바이스 ↔ 브로커: **TLS + per-device 토큰** (bcrypt 해시 DB 저장)
- 앱 ↔ DB: **HTTPS + JWT + RLS** (본인 데이터만 접근)
- 영상: **버킷 비공개 + presigned URL** (TTL 1시간)
- 명령: **msg_id 중복 제거 + TTL 검증** (replay/지연 명령 방어)
- 안전 액추에이터 (히터): **펌웨어 물리 안전망 독립** (클라우드 명령과 무관)

## 팀원별 첫 읽을 문서

| 역할 | 추천 순서 |
|------|----------|
| **PM/기획자** | OVERVIEW.md (이 문서) → [cloud_integration.md](../esp32/NimBLE_Connection/docs/cloud_integration.md) 섹션 1~3 |
| **백엔드** | OVERVIEW → [ARCHITECTURE.md](docs/ARCHITECTURE.md) → [DATABASE.md](docs/DATABASE.md) → [API.md](docs/API.md) → [specs/](specs/) |
| **펌웨어 (센서)** | OVERVIEW → [cloud_integration.md](../esp32/NimBLE_Connection/docs/cloud_integration.md) → [MQTT.md](docs/MQTT.md) |
| **카메라 워커 (ESP32-P4)** | OVERVIEW → [stage-f-camera-ingest.md](specs/stage-f-camera-ingest.md) → [stage-g-live-streaming.md](specs/stage-g-live-streaming.md) → [MQTT.md](docs/MQTT.md) |
| **앱 개발자** | OVERVIEW → [DATABASE.md](docs/DATABASE.md) (RLS/Realtime 필수) → [API.md](docs/API.md) |
| **인프라/DevOps** | OVERVIEW → [DEPLOYMENT.md](docs/DEPLOYMENT.md) → [ENV.md](docs/ENV.md) |

## 관련 레포

- **이 레포 (terra-server)** — 백엔드 (FastAPI + MQTT 브리지)
- **펌웨어 (센서/제어, ESP32-S3)** — `~/project/esp32/NimBLE_Connection`
- **펌웨어 (카메라, ESP32-P4)** — `~/project/esp32/terra-cam-p4` (Stage F 시작 시 부트스트랩 예정)
- **대안 카메라 워커 (RPi)** — `~/project/terra-cam-pi` (필요 시 추후)
- **참조 레포** — `~/project/petcam-lab` (FastAPI/Supabase/R2/motion 패턴 차용)
