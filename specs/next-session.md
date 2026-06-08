# 다음 세션 시작점

> 새 Claude 가 처음 읽을 문서. "뭐부터 해야 해?" 답변.

## 현재 상태 (2026-06-08)

### 백엔드 완성도

| 모듈 | 상태 |
|------|------|
| `backend/auth.py` (JWT) | ✅ |
| `backend/auth_camera.py` (Camera Token) | ✅ |
| `backend/supabase_client.py` | ✅ |
| `backend/crypto.py` (bcrypt) | ✅ |
| `backend/r2_client.py` (presigned URL) | ✅ |
| `backend/health.py` | ✅ |
| `backend/main.py` (FastAPI, docs basic auth, /web 마운트) | ✅ |
| `backend/routers/devices.py` | ✅ |
| `backend/routers/enclosures.py` | ✅ |
| `backend/routers/cameras.py` | ✅ |
| `backend/routers/clips.py` | ✅ |
| `backend/mqtt/topics.py` | ✅ |
| `backend/mqtt/bridge.py` (paho-mqtt) | ✅ |
| `backend/mqtt/handlers.py` (telemetry/ack/alert + alerts hook) | ✅ |
| `backend/mqtt/dispatcher.py` (Stage C 명령 디스패처 polling) | ✅ |
| `backend/alerts.py` (Stage D 임계값 평가 + hysteresis) | ✅ **(2026-06-08 신규)** |
| `backend/offline_monitor.py` (Stage D last_seen 감시) | ✅ **(2026-06-08 신규)** |
| `backend/mqtt_bridge_main.py` (bridge + dispatcher + offline_monitor 통합) | ✅ **(2026-06-08 통합)** |

### 웹 콘솔 완성도 (web/index.html)

| 기능 | 상태 |
|------|------|
| Supabase Auth (회원가입/로그인) | ✅ |
| Enclosure CRUD | ✅ |
| Device 페어링/목록/삭제 + MQTT 토큰 1회 노출 | ✅ |
| Camera 페어링/목록/삭제 + Camera 토큰 1회 노출 | ✅ |
| 명령 발행 (`commands` INSERT) + 최근 명령 패널 | ✅ |
| 실시간 telemetry (Supabase Realtime 구독) | ✅ **(2026-06-08 신규)** |
| 활성 알림 (alerts) 패널 + 수동 해제 | ✅ **(2026-06-08 신규)** |
| 모션 클립 재생 | ❌ (Stage F2 R2 동작 확인 후) |
| 라이브 스트리밍 | ❌ (Stage G) |

### Stage 진행

| Stage | 상태 |
|-------|------|
| A — MQTT 브리지 telemetry → DB | ✅ |
| B — BLE + JWT 페어링 (백엔드) | ✅ / 펌웨어 측 ❌ |
| C — 명령 디스패치 (DB → MQTT publish) | ✅ |
| D — 알림 (임계값 + offline) | ✅ |
| E — 시계열 다운샘플 (pg_cron) | ❌ |
| F — ESP32-P4 카메라 영상 인제스트 | 백엔드 ✅ / 펌웨어 ❌ |
| G — 라이브 스트리밍 (G1 snapshot / G2 WebRTC) | ❌ |

## 우선순위 작업

### 1순위: 동작 검증 (사람 작업)
- [ ] 웹에서 디바이스 페어링 → `mosquitto_pub` 으로 가짜 telemetry → DB 행 + 실시간 차트 표시 확인
- [ ] `device_settings` 임계값 설정 후 임계 초과 telemetry → alerts INSERT 확인
- [ ] 디바이스 3분간 telemetry 안 보내기 → offline alert 발생 확인
- [ ] 명령 발행 → `mosquitto_sub` 으로 `esp32/{device_id}/command` 수신 확인
- [ ] 가짜 ack publish → commands.status='acked' UPDATE 확인

### 2순위: 펌웨어 작업 (Claude 또는 사람, 별도 레포)
- [ ] `~/project/esp32/NimBLE_Connection` 에 `cloud_client.c` 추가 (Stage B 디바이스 측)
  - BLE 페어링 → terra-api POST /devices/pair
  - MQTT 클라이언트 (paho 대신 esp-mqtt) — telemetry publish, command subscribe, ack publish
  - 안전 액추에이터(히터): TTL/msg_id 중복 제거 + 물리 latch 유지
- [ ] `~/project/esp32/terra-cam-p4` 신규 ESP32-P4 카메라 워커 펌웨어
  - ESP-IDF v5.3+ + esp_video + esp_h264 + esp_mp4 + esp-mqtt + nimble
  - 모션 감지 + H.264 10초 mp4 + R2 PUT + motion_event publish

### 3순위: Stage E (시계열 다운샘플)
- [ ] `migrations/YYYY-MM-DD_pgcron_downsample.sql`
  - pg_cron 활성화 + 매분 telemetry → telemetry_1m UPSERT
  - 매시간 telemetry 7일 이전 DELETE

### 4순위: Stage G (라이브 스트리밍)
- [ ] G1: snapshot 라우터 + 워커 펌웨어 측 1초 JPEG 캡처
- [ ] G2: webrtc 시그널링 릴레이 + esp_webrtc 통합

## 미결정 항목 (참고)

| # | 항목 | 현재 디폴트 | 결정 시점 |
|---|------|-------------|----------|
| B1 | 사육 종 별 환경 프리셋 | species 텍스트 필드만 | Stage B 완료 후 |
| C1-C4 | 펌웨어 측 결정 (토큰 주입, SNTP, cJSON, LAN HTTP) | 펌웨어 작업 시 | 진행 중 |
| D2 | 알림 채널 (FCM/이메일/SMS) | 현재 alerts 테이블 + Realtime 뿐 → 푸시 미도입 | 추후 |
| F2 | 영상 보존/즐겨찾기 정책 | 30일 자동 삭제 + favorite 검토 | Stage F2 |
| G1 | 라이브 JPEG snapshot 간격 | 1초 권장 | Stage G1 |
| G2 | WebRTC TURN | Google STUN → 필요 시 coturn | Stage G2 |

## 환경 점검 (세션 시작 시 확인)

```bash
cd ~/project/terra-server
uv run python --version    # 3.12.x
uv sync
uv run pytest              # 현재 테스트 7개 모듈 (test_auth_camera, test_cameras_api, test_clips_api, test_enclosures_api, test_mqtt_dispatcher, test_mqtt_handlers, test_r2_client)

# 로컬 가동 (API)
uv run uvicorn backend.main:app --reload

# 로컬 가동 (브리지 + 디스패처 + offline 모니터, Mosquitto 가 떠 있어야 함)
uv run terra-bridge

# 웹
python3 -m http.server -d web 5500
```

## 관련 레포

- 펌웨어 (센서/제어, ESP32-S3): `~/project/esp32/NimBLE_Connection`
- 펌웨어 (카메라, ESP32-P4): `~/project/esp32/terra-cam-p4` (Stage F 시점에 부트스트랩)
- 대안 카메라 워커 (RPi): `~/project/terra-cam-pi` (필요 시)
- 참조 패턴: `~/project/petcam-lab` (FastAPI/Supabase/R2/motion 패턴 차용)
- 기획서: `~/project/esp32/NimBLE_Connection/docs/cloud_integration.md`
