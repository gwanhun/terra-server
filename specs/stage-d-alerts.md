# Stage D — 알림 (alerts INSERT + 푸시) (⏸️ 보류)

## 개요

이상 상황 발생 시 `alerts` 테이블에 INSERT, 사용자에게 푸시 알림 발송. 알림 종류:
- 디바이스 측 즉시 알림: `heater_latched`, `sensor_fault` (MQTT alert topic)
- 브리지 측 평가 알림: `temp_high`, `temp_low`, `humid_low`, `offline`

## In

- Stage A, C 완료
- (옵션) FCM 서비스 계정 키 발급
- `device_settings` 에 임계값 입력 (앱에서)

## Out

- 알림 UI / 사용자 설정 화면
- 이메일/SMS 알림
- 알림 재발송 정책 (cool-down)

## 완료 조건

- [ ] `backend/mqtt/bridge.py` `_handle_alert` 실제 구현 (디바이스 발 알림)
  - [ ] alerts INSERT
  - [ ] (옵션) FCM 푸시 트리거
- [ ] `backend/alerts.py` 신규 (브리지 측 평가)
  - [ ] telemetry 수신 시 device_settings 조회 → 임계값 비교
  - [ ] 임계값 초과 시 alerts INSERT (kind='temp_high' 등)
  - [ ] 동일 kind 활성 알림 있으면 dedup (중복 INSERT 방지)
  - [ ] 정상화 시 resolved_at 자동 갱신
- [ ] `backend/offline_monitor.py` 신규
  - [ ] 1분 주기로 devices.last_seen_at 검사
  - [ ] 3분 이상 무응답 → is_online=false + alerts INSERT (kind='offline')
- [ ] (옵션) FCM 푸시 통합

## 설계 메모

### 알림 dedup
같은 디바이스 + 같은 kind 의 활성 알림이 있으면 새로 INSERT 안 함 (소음 방지). resolved_at IS NULL 인 행 SELECT 로 확인. 정상화되면 자동 resolve.

### 이력 vs 활성 알림
`alerts` 한 테이블에 다 저장. resolved_at NULL = 활성, NOT NULL = 이력. 앱은 활성만 푸시 / 이력은 화면에서 조회.

### offline 감지 임계
last_seen_at 기준:
- 1분 = 정상 변동 범위 (3초 주기 × 20 마진)
- **3분 = offline 으로 판단** (네트워크 일시 끊김 vs 진짜 다운)
- 10분 = critical (장기 장애)

### FCM 도입 시점
초기엔 Supabase Realtime 으로 알림 화면 갱신만. 푸시 알림은 사용자 요청 늘면 추가 (FCM 서비스 계정, 토큰 관리, iOS APNs 등 작업 큼).

## 학습 노트

### Supabase trigger 안 쓰는 이유
DB trigger 로 alerts INSERT 가능하지만, FCM 호출은 외부 HTTP 라 Supabase Edge Function 필요. 복잡도 증가. → 브리지(Python) 안에서 평가 + INSERT + FCM 호출 통합이 단순.

## 참고

- `device_settings.alert_temp_high` 등 임계값 컬럼
- [docs/MQTT.md](../docs/MQTT.md) — alert payload
