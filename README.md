# terra-server

파충류/양서류 **사육장 통합 백엔드**.
하나의 사육장 = ESP32-S3(센서/제어) + ESP32-P4(카메라 영상/라이브). 둘 다 하나의 서버에서 처리.

> 펌웨어 레포 (센서/제어): `~/project/esp32/NimBLE_Connection`
> 펌웨어 레포 (카메라): `~/project/esp32/terra-cam-p4` (예정, 신규)
> 대안 카메라 워커 (RPi): `~/project/terra-cam-pi` (필요 시 추후)
> 참조 패턴 레포: `~/project/petcam-lab` (FastAPI + Supabase + R2 + motion 패턴 차용)

## 한 줄 요약

```
ESP32-S3 (센서/제어) ──MQTTS──┐
                              ├─→ Mosquitto ─→ terra-bridge ─→ Supabase
ESP32-P4 (카메라) ────MQTTS──┘                              ↑
+ MIPI Camera                + HTTPS R2 PUT ─→ Cloudflare R2 ┘
                             + WebRTC P2P (라이브, Stage G2)  ↓ presigned URL
                                                              앱 (Realtime)
```

## 처리하는 도메인

| 도메인 | 입력 | 처리 | 저장 |
|--------|------|------|------|
| **IoT 제어** | ESP32-S3 telemetry/ack/alert | MQTT 브리지 → Supabase | Supabase (telemetry, commands, alerts) |
| **모션 영상 (Stage F)** | ESP32-P4 motion 알림 + H.264 mp4 | presigned URL 발급 → R2 메타 등록 | R2 (영상, ~1MB/클립) + Supabase (메타) |
| **라이브 스트리밍 (Stage G)** | G1: JPEG / G2: WebRTC offer | snapshot R2 / 시그널링 릴레이 | R2 latest.jpg (G1) / P2P (G2, 저장 없음) |

→ terra-server 는 **영상 분석은 하지 않음**. ESP32-P4 워커가 자체적으로 모션 감지 + H.264 인코딩 + R2 PUT + WebRTC stream.

## 인프라

| 항목 | 값 |
|------|------|
| 호스팅 | AWS Lightsail (Ubuntu 22.04, 서울 리전, $3.50/월) |
| 브로커 | Mosquitto 2.x (TLS, 8883) |
| API 서버 | Python 3.12 + FastAPI + uvicorn |
| 브리지 | paho-mqtt (단일 프로세스, systemd) |
| DB | Supabase (Postgres + Auth + RLS + Realtime) |
| 영상 스토리지 | Cloudflare R2 (S3 호환, presigned URL) |
| TLS | Let's Encrypt (certbot) |
| 프로세스 관리 | systemd |

## 빠른 시작 (로컬 개발)

```bash
# 1. Python 3.12 + uv 설치 (없으면)
brew install uv
uv python install 3.12

# 2. 의존성 설치
cd ~/project/terra-server
uv sync

# 3. 환경변수 설정
cp .env.example .env
# .env 파일 열어 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY 등 입력

# 4. Supabase 마이그레이션 실행 (순서대로 적용)
# Supabase 대시보드 > SQL Editor 에 아래 순서대로 붙여넣기
#   1) migrations/2026-05-26_initial_schema.sql   (IoT 테이블)
#   2) migrations/2026-05-26_camera_schema.sql    (카메라/영상 테이블)

# 5. API 서버 실행
uv run uvicorn backend.main:app --reload

# 6. (별도 터미널) MQTT 브리지 실행
uv run terra-bridge
```

## 폴더 구조

```
terra-server/
├── backend/
│   ├── main.py                  # FastAPI 진입점
│   ├── mqtt_bridge_main.py      # MQTT 브리지 진입점
│   ├── auth.py                  # JWT 검증 (Supabase Auth)
│   ├── supabase_client.py       # service_role 싱글톤
│   ├── crypto.py                # 디바이스/카메라 토큰 bcrypt
│   ├── r2_client.py             # R2 presigned URL 발급 (Stage F)
│   ├── health.py                # /health
│   ├── routers/
│   │   ├── devices.py           # /devices CRUD + /pair
│   │   ├── enclosures.py        # /enclosures CRUD (사육장 묶음, Stage F)
│   │   ├── cameras.py           # /cameras CRUD + /pair (Stage F)
│   │   └── clips.py             # /clips 메타 + presigned URL (Stage F)
│   └── mqtt/
│       ├── topics.py            # 토픽 상수
│       └── bridge.py            # paho-mqtt 클라이언트
├── migrations/                  # Supabase SQL 마이그레이션
├── tests/                       # pytest
├── specs/                       # 스테이지별 스펙
├── docs/                        # 공식 문서
├── scripts/                     # 일회성 유틸 스크립트
├── pyproject.toml
├── .env.example
├── CLAUDE.md / AGENTS.md
└── README.md
```

## 문서

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 시스템 맵
- [docs/DATABASE.md](docs/DATABASE.md) — 테이블/RLS
- [docs/MQTT.md](docs/MQTT.md) — 토픽/페이로드 명세
- [docs/API.md](docs/API.md) — REST API 명세 (Swagger UI: `/docs`)
- [docs/ENV.md](docs/ENV.md) — 환경변수 가이드
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Lightsail 인프라 초기 셋업
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — 일상 운영 (배포/재배포/로그, pm2 ↔ systemd 매핑)
- [docs/FIRMWARE_INTEGRATION.md](docs/FIRMWARE_INTEGRATION.md) — **펌웨어 AI 의 단일 진실 소스** (페어링 흐름 + MQTT 토픽 + 페이로드 + 체크리스트)
- [docs/APP_INTEGRATION.md](docs/APP_INTEGRATION.md) — **앱 AI/개발자의 단일 진실 소스** (Supabase Auth + REST + Realtime + BLE 페어링 + 명령 발행)
- [scripts/sim_device.py](scripts/sim_device.py) — Python ESP32 시뮬레이터 (참조 구현 + 백엔드 검증)

## 라이선스

Private.
