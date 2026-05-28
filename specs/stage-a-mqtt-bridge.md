# Stage A — MQTT 브리지 telemetry 수신 (🚧 진행 중)

## 개요

ESP32 가 publish 하는 `esp32/+/telemetry` 메시지를 Mosquitto 에서 받아 Supabase `telemetry` 테이블에 INSERT 한다. 동시에 `devices.last_seen_at` / `is_online` 갱신.

## In (선행 조건)

- Lightsail 인스턴스 가동, Mosquitto TLS 동작 중 ([docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md))
- Supabase 신규 프로젝트 생성 + [migrations/2026-05-26_initial_schema.sql](../migrations/2026-05-26_initial_schema.sql) 적용
- `.env` 작성 (Supabase URL/key, MQTT 브리지 계정)
- 테스트용 디바이스 1대 (수동으로 `devices` 테이블에 INSERT 또는 Stage B 완료 후)

## Out (스코프 밖)

- 페어링 흐름 (Stage B)
- 명령 발행 (Stage C)
- 알림 발생 로직 (Stage D)
- 다운샘플 (Stage E)

## 완료 조건

- [ ] `backend/mqtt/bridge.py` `_handle_telemetry` 실제 구현
  - [ ] payload 파싱 (dht22_a/b, relay/fan/heater/ts)
  - [ ] `devices` 조회 (device_id → UUID)
  - [ ] `telemetry` INSERT
  - [ ] `devices.last_seen_at`, `is_online=true` UPDATE
- [ ] 단위 테스트 (`tests/test_mqtt_bridge.py`)
  - [ ] paho mock 으로 메시지 수신 시뮬레이션
  - [ ] payload 정상 / 형식 불량 / device_id 미존재 케이스
- [ ] systemd 가동 후 `mosquitto_pub` 으로 더미 메시지 → DB 행 확인
- [ ] `journalctl -u terra-bridge -f` 로그에 INSERT 성공 확인

## 설계 메모

### 왜 단일 브리지 프로세스?
디바이스 수 100대 미만에서는 paho-mqtt 단일 스레드로 충분. 부하 늘면 telemetry/command/alert 핸들러를 별도 프로세스로 분리 (asyncio queue + 멀티 워커).

### Supabase INSERT 비용
3초 × 디바이스 10대 = 초당 ~3.3 req. Supabase Free tier 권장 limit 안. 100대 넘으면 1초 배치 INSERT 로 묶기.

### device_id ↔ UUID 캐시
매 메시지마다 `devices` 조회는 낭비. `device_id → UUID` 메모리 캐시 (LRU, 1000 entries). 페어링 / 토큰 회전 시 invalidate.

## 학습 노트

### paho-mqtt CallbackAPIVersion.VERSION2
paho 2.0+ 에서 콜백 시그니처 변경. `(client, userdata, flags, reason_code, properties)` 로 통일. 1.x 코드와 호환 안 됨.

### Mosquitto QoS 0 의 의미
brokered delivery 보장 X. ESP32 publish → 네트워크 끊김 → 브로커 도달 못 함. telemetry 는 손실 허용이라 QoS 0 사용. ack/command 는 QoS 1.

## 참고

- petcam-lab `backend/encode_upload_worker.py` (asyncio 워커 패턴)
- [docs/MQTT.md](../docs/MQTT.md) — 페이로드 스키마
