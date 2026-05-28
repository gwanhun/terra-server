# Stage B — 디바이스 페어링 (BLE + JWT) (⏸️ 보류)

## 개요

신규 ESP32 가 부팅 후 BLE 광고 → 앱이 사용자 JWT + WiFi 정보 전달 → ESP32 가 WiFi 연결 후 `POST /devices/pair` 호출 → 서버가 device 등록 + MQTT 토큰 발급 → ESP32 가 NVS 저장 후 MQTT 연결.

## In

- Stage A 완료 (브리지가 telemetry 수신 가능)
- ESP32 펌웨어에 BLE 프로비저닝 + HTTPS 클라이언트 + NVS 저장 로직 (펌웨어 작업)
- 앱이 Supabase Auth 로그인 + BLE write 가능

## Out

- 페어링 UI 디자인
- QR 코드 페어링 (대안 — 차후 검토)
- 토큰 회전 흐름 (Stage D 에서 다룸)

## 완료 조건

- [ ] `backend/routers/devices.py` `POST /devices/pair` 동작 검증
  - [x] 기본 구현 완료 (Stage 부트스트랩에서)
  - [ ] JWT 검증 통합 테스트
  - [ ] device_id 충돌 시 재시도
- [ ] Mosquitto 동적 ACL 업데이트
  - [ ] `scripts/regenerate_mosquitto_acl.py` 작성
  - [ ] 페어링 성공 후 자동 호출 (또는 cron 5분 주기)
- [ ] 펌웨어 측 `cloud_client.c` 페어링 로직 구현 (펌웨어 레포)
- [ ] 통합 시나리오: 전원 ON → 앱 BLE 연결 → 페어링 → MQTT 연결 → telemetry 수신
- [ ] tests/test_devices_api.py

## 설계 메모

### MQTT 토큰을 평문으로 1회만 노출
페어링 응답에 평문 `mqtt_token` 포함 → ESP32 NVS 저장. DB 에는 bcrypt 해시. 분실 시 재페어링 필수 (보안).

### Mosquitto ACL 동적 업데이트
페어링마다 디바이스별 ACL 라인 추가 필요. 옵션:
1. **파일 직접 수정 + SIGHUP** — 즉시 반영, 파일 락 조심
2. **cron 5분 주기 재생성** — 단순, 지연 감수
3. **mosquitto-go-auth + DB lookup** — 가장 깔끔, 추가 컴포넌트 (배보다 배꼽)

→ 1번 (직접 수정 + reload) 권장.

### 페어링 race condition
같은 device_id 가 두 번 INSERT 시도 가능 (재시도). DB UNIQUE 제약 위반 시 `secrets.token_hex(4)` 재생성 → 최대 3회 재시도.

## 학습 노트

### Pydantic v2 model_dump(exclude_unset=True)
PATCH 요청에서 "전송된 필드만" 추출. UPDATE SQL 에 None 으로 덮어쓰기 방지.

### Supabase service_role + RLS
`service_role` 키는 RLS 우회 → INSERT 시 `owner_id` 명시 필수. 빠뜨리면 NULL → NOT NULL 위반.

## 참고

- petcam-lab `backend/routers/cameras.py` (Pydantic 라우터 패턴)
- 펌웨어 `~/project/esp32/NimBLE_Connection/main/src/gatt_svc.c`
