# 다음 세션 시작점

> 새 Claude 가 처음 읽을 문서. "뭐부터 해야 해?" 답변.

## 현재 상태 (2026-05-26)

부트스트랩 완료:
- ✅ 폴더 구조 + `pyproject.toml` + `.env.example`
- ✅ 초기 마이그레이션 SQL (devices, telemetry, commands, alerts)
- ✅ 카메라 마이그레이션 SQL (enclosures, cameras, motion_clips)
- ✅ 백엔드 모듈 스켈레톤 (auth, supabase_client, crypto, health, mqtt/bridge, routers/devices)
- ✅ 문서 (ARCHITECTURE, DATABASE, MQTT, ENV, DEPLOYMENT)
- ✅ 스테이지 스펙 6개 (A~F)

**확정된 큰 그림** (2026-05-27 갱신):
- 하드웨어 분리: ESP32-S3 (센서/제어) + **ESP32-P4** (카메라/영상/라이브) — 모두 ESP-IDF/MCU
  - 대안: Raspberry Pi Zero 2 W (cloud_integration.md 0장에 옵션 유지)
- 단일 백엔드: 두 종류 디바이스 모두 terra-server 가 처리
- 영상: H.264 mp4 HD 720p 10초 (ESP32-P4 esp_h264 HW 인코더), R2 직접 PUT (terra-server 는 presigned URL 발급만, 분석 X)
- 페어링: 두 종류 모두 **BLE** (ESP32-P4 도 BLE 5.0, NimBLE 공통)
- 라이브 스트리밍: Stage G1 (JPEG snapshot) → Stage G2 (WebRTC P2P) 단계적
- 상위 묶음: `enclosures` 테이블 도입 (선택적 — 단독 디바이스도 허용)

## 우선순위 작업

### 1순위: 인프라 셋업 (사람 작업)
- [ ] AWS Lightsail 인스턴스 생성 (서울, $3.50, Ubuntu 22.04)
- [ ] 정적 IP 할당
- [ ] 도메인 DNS 연결 (mqtt.example.com, api.example.com)
- [ ] [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) 따라 셋업

### 2순위: Supabase 셋업 (사람 작업)
- [ ] Supabase 신규 프로젝트 생성 (petcam-lab 과 별개)
- [ ] 마이그레이션 순서대로 적용:
  - 1) `migrations/2026-05-26_initial_schema.sql`
  - 2) `migrations/2026-05-26_camera_schema.sql`
- [ ] `.env` 작성 (SUPABASE_URL, SERVICE_ROLE_KEY, DEV_USER_ID)

### 3순위: Cloudflare R2 셋업 (사람 작업, Stage F 진입 시)
- [ ] R2 버킷 생성 (`terra-clips`, 비공개)
- [ ] API 토큰 발급 (Object R/W, 해당 버킷만)
- [ ] `.env` 에 R2 변수 입력
- [ ] (옵션) Lifecycle rule 추가 (30일)

### 4순위: Stage A 구현 (Claude 작업)
- [ ] `backend/mqtt/bridge.py` `_handle_telemetry` 실제 구현
- [ ] `tests/test_mqtt_bridge.py` 작성
- [ ] 통합 테스트 (`mosquitto_pub` → DB 행 확인)

자세한 내용: [stage-a-mqtt-bridge.md](stage-a-mqtt-bridge.md)

### 5순위: Stage F 구현 (Claude 작업, Stage B 완료 후)
- [ ] `backend/r2_client.py` 신규
- [ ] `backend/routers/{enclosures,cameras,clips}.py` 신규
- [ ] 카메라 토큰 인증 미들웨어
- [ ] tests 작성

자세한 내용: [stage-f-camera-ingest.md](stage-f-camera-ingest.md)

### 별도 트랙: 디바이스 측 작업 (Claude 또는 사람)
- [ ] `~/project/esp32/NimBLE_Connection` 에 `cloud_client.c` 추가 (Stage B 진행 시)
- [ ] `~/project/esp32/terra-cam-p4` 신규 ESP32-P4 카메라 워커 펌웨어 레포 부트스트랩 (Stage F 진행 시)
  - ESP-IDF v5.3+ + esp_video + esp_h264 + esp_mp4 + esp-mqtt + nimble
  - 모션 알고리즘은 petcam-lab motion.py 패턴을 C 로 포팅
- [ ] (옵션) `~/project/terra-cam-pi` RPi 워커 레포 (대안 워커, 필요 시)

## 미결정 항목 (참고)

| # | 항목 | 현재 디폴트 | 결정 시점 |
|---|------|-------------|----------|
| B1 | 사육 종 별 환경 프리셋 | species 텍스트 필드만 | Stage B 완료 후 |
| C1-C4 | 펌웨어 측 결정 (토큰 주입, SNTP, cJSON, LAN HTTP) | 펌웨어 작업 시 | Stage B 와 병행 |
| D1 | 알림 워커 분리 여부 | bridge 통합 | Stage D 시작 시 |
| D2 | 알림 채널 (FCM/이메일/SMS) | FCM 우선 | Stage D 중반 |
| F1 | ESP32-P4 카메라 워커 구체 사양 | ESP32-P4 (FireBeetle 2) + MIPI 카메라 + HD 720p 10초 @ 24fps + BLE 페어링 (확정) | 확정 |
| F2 | 영상 보존/즐겨찾기 정책 | 30일 후 자동 삭제 + favorite 플래그 검토 | Stage F2 진행 시 |
| G1 | 라이브 JPEG snapshot 간격 | 1초 권장 (변경 가능) | Stage G1 진행 시 |
| G2 | WebRTC TURN 서버 운영 | Google STUN 만으로 시작 → 필요 시 coturn on Lightsail | Stage G2 진행 시 |

## 환경 점검 (세션 시작 시 확인)

```bash
cd ~/project/terra-server
uv run python --version  # Python 3.12.x
uv sync
uv run pytest  # 현재 0 tests (스텁만)
```

## 관련 레포

- 펌웨어 (센서/제어, ESP32-S3): `~/project/esp32/NimBLE_Connection`
- 펌웨어 (카메라, ESP32-P4): `~/project/esp32/terra-cam-p4` (Stage F 에서 부트스트랩)
- 대안 카메라 워커 (RPi): `~/project/terra-cam-pi` (필요 시 추후)
- 참조 패턴: `~/project/petcam-lab` (FastAPI/Supabase/R2/motion 패턴 차용)
- 기획서: `~/project/esp32/NimBLE_Connection/docs/cloud_integration.md`
