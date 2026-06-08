# 펌웨어 AI 에게 줄 프롬프트 (복붙용)

> 같은 컴퓨터의 다른 Claude 인스턴스 (펌웨어 작업) 에게 그대로 복사해서 붙여넣기.
> 각 섹션은 독립 단위. 한 번에 하나씩 진행.

---

## 🚀 0. 첫 컨텍스트 (제일 먼저 한 번만)

```text
지금부터 ESP32-S3 펌웨어 작업이야. 같은 컴퓨터에 두 프로젝트가 있어:

1. 백엔드 (terra-server):
   - 경로: ~/project/terra-server
   - 인터페이스 명세: ~/project/terra-server/docs/FIRMWARE_INTEGRATION.md  ← 단일 진실 소스
   - Python 참조 구현: ~/project/terra-server/scripts/sim_device.py  ← 동작 ground truth
   - 백엔드 운영 중: https://api.terra-server.uk + mqtt.terra-server.uk:8883 (TLS)

2. 펌웨어 (현재 작업):
   - 경로: ~/project/esp32/NimBLE_Connection
   - 이미 있는 것: BLE GAP/GATT, WiFi, MQTT (mqtt_app.c — sdkconfig 자격증명 사용), 센서/액추에이터/LED/디스플레이
   - 빠진 것: cloud_client.c (HTTPS POST /devices/pair), NVS 통합 (device_id + mqtt_token 동적 저장/로드), main.c 부팅 흐름 (NVS 있으면 MQTT 바로, 없으면 BLE 페어링)

작업 순서:
  ① cloud_client.c + NVS creds 모듈
  ② mqtt_app.c 자격증명을 NVS 에서 로드 (sdkconfig CONFIG_APP_MQTT_PASSWORD 제거)
  ③ main.c 부팅 분기

먼저 두 문서 다 읽고 (FIRMWARE_INTEGRATION.md + sim_device.py) 현재 NimBLE_Connection 의
main.c / mqtt_app.c / wifi.c / gatt_svc.c 도 확인해서 어디 통합할지 짚어줘.
코드 짜기 전에 짚은 부분 + 작업 계획 먼저 보여주고 OK 받고 진행.
```

---

## ① cloud_client.c — HTTPS POST + JSON 파싱

```text
첫 작업: cloud_client.c / cloud_client.h 신규.

요구:
- 함수: esp_err_t cloud_pair(const char *jwt, const char *name,
                              char *out_device_id, size_t out_device_id_len,
                              char *out_mqtt_token, size_t out_mqtt_token_len);
- esp_http_client + esp_crt_bundle (Let's Encrypt 인증) + cJSON 사용
- POST https://api.terra-server.uk/devices/pair
  Headers: Authorization: Bearer <jwt>, Content-Type: application/json
  Body: {"name":"<name>","firmware_ver":"terra-fw 0.1.0"}
- 응답 201 + JSON 파싱 → out_device_id, out_mqtt_token 채움
- 에러 처리: 401 (JWT 만료) / 422 (필드) / 5xx (재시도) 구분 로그

검증:
- ~/project/terra-server/scripts/sim_device.py 의 pair_device() 와 동일 동작이어야 함
- 시뮬레이터로 같은 흐름 미리 테스트 가능 (UART 로그 비교 가능하게 ESP_LOGI 풍부하게)

빌드 가능한 상태로 마무리해서 idf.py build 통과까지.
```

---

## ② creds_store.c — NVS 저장/로드

```text
다음 작업: creds_store.c / creds_store.h 신규 (이미 NVS 쓰는 곳 있으면 그쪽에 추가).

요구:
- namespace: "terra_creds"
- 키: "device_id" (string), "mqtt_token" (string), 옵션 "owner_uuid"
- 함수:
  esp_err_t creds_save(const char *device_id, const char *mqtt_token);
  esp_err_t creds_load(char *device_id, size_t did_len,
                       char *mqtt_token, size_t tok_len);
  bool creds_exist(void);
  esp_err_t creds_clear(void);    // 펑크 페어링 시 사용
- creds_load 가 ESP_OK 면 NVS 있음, ESP_ERR_NVS_NOT_FOUND 면 없음
- chmod 같은 권한은 NVS encryption 권장 (선택, 일단 평문 OK)

빌드 통과 + 단위 동작 확인 (test_creds.c 같은 거 만들거나 main 에서 더미 호출).
```

---

## ③ mqtt_app.c 수정 — 자격증명 동적화

```text
mqtt_app.c 가 현재 CONFIG_APP_MQTT_PASSWORD (sdkconfig 컴파일 시 박힘) 사용 중.
이걸 NVS 에서 로드한 device_id + mqtt_token 으로 교체.

요구:
- mqtt_app_init(const char *device_id, const char *mqtt_token) 시그니처로 변경
  (또는 mqtt_app_start(device_id, token))
- esp-mqtt config 의 client_id / credentials.username / credentials.password 동적 채움
- broker host/port 도 NVS 또는 sdkconfig 에서 (host 는 고정 OK: mqtt.terra-server.uk:8883)
- TLS: esp_crt_bundle (Let's Encrypt) 사용

기존 sdkconfig 의 APP_MQTT_PASSWORD 항목 + Kconfig.projbuild 의 관련 부분 제거 가능.
```

---

## ④ main.c 부팅 분기

```text
main.c 부팅 흐름 정리:

1. NVS 초기화 (nvs_flash_init)
2. creds_load 시도
3a. 있음 → wifi_connect (sdkconfig SSID/PW — 현재 고정) → mqtt_app_init(device_id, token) → done
3b. 없음 → BLE 페어링 모드 (NimBLE GAP/GATT 광고)
    - 앱이 BLE 로 ssid, password, jwt, name 전달
    - WiFi 연결
    - cloud_pair(jwt, name) → device_id + mqtt_token 받음
    - creds_save
    - mqtt_app_init → done

WiFi 는 현재 고정 (사용자가 sdkconfig 또는 main.c 에 박아둠). BLE 단계에서 받는 ssid/password 는 일단 무시해도 OK (나중에 확장).

테스트:
- 1차: NVS clear → 부팅 → BLE 페어링 흐름 → 시뮬레이터 sim_device.py 와 같은 로그 패턴 확인
- 2차: 재부팅 → 페어링 스킵하고 바로 MQTT 연결 + telemetry publish 확인
```

---

## ⑤ telemetry/command/ack publish (이미 있으면 단순 보강)

```text
mqtt_app.c 에 이미 telemetry publish 가 있을 가능성 큼. FIRMWARE_INTEGRATION.md §5.3 의
JSON 포맷과 정확히 일치하는지 확인 + 안 맞으면 수정.

command subscribe + ack publish 추가 (없으면):
- subscribe: esp32/{device_id}/command (QoS 1)
- msg_id 링버퍼 (최근 8개, 중복이면 ack rejected_duplicate_msg_id)
- TTL 검증 (now - issued_at > ttl_sec → rejected_ttl_expired)
- action 디스패치 (relay_toggle / fan_toggle / heater_toggle / heater_clear / led_on/up/down)
- heater 안전 latch (90초 연속 가동 → safety latch + alert publish)
- ack publish: esp32/{device_id}/ack (QoS 1, retain=false)

JSON 포맷은 sim_device.py 의 telemetry_payload() / apply_command() / heater latch 그대로 보면 됨.

검증:
- 백엔드 https://api.terra-server.uk 의 웹 콘솔에서 명령 발행
- ESP32 가 받고 ack publish 하면 웹 "최근 명령" 패널이 pending → sent → acked
```

---

## 🔁 펌웨어 ↔ 백엔드 통합 검증 흐름

펌웨어 AI 가 작업 끝나면 너 (백엔드 측) 가 검증:

```bash
# 1. Supabase 로그인해서 JWT 얻기 (브라우저 console 또는 웹 콘솔)
JWT="eyJ..."

# 2. 먼저 시뮬레이터로 백엔드 정상 동작 확인 (ground truth)
cd ~/project/terra-server
uv run python scripts/sim_device.py --pair --jwt "$JWT" --name "GROUND" --state-file .sim_ground.json
# → 페어링 + MQTT 연결 + 3초마다 telemetry publish 로그 보임
# Ctrl+C

# 3. 같은 JWT 로 실제 ESP32 펌웨어 부팅 + 페어링 시도
# (펌웨어 AI 가 BLE 로 JWT 받는 흐름 만들었으면 nRF Connect 같은 도구로 전달)

# 4. 결과 비교:
#   - 백엔드 web 콘솔의 디바이스 목록에 둘 다 행 추가됨?
#   - 텔레메트리 패널에 둘 다 데이터 들어옴?
#   - 명령 발행 시 둘 다 ack 보내?
```

차이 나면 sim_device.py 의 코드와 펌웨어 코드 옆에 놓고 비교 (포맷 차이, 토픽 오타 등 잡기 쉬움).

---

## 🆘 막힐 때 펌웨어 AI 에게 추가 컨텍스트

```text
백엔드 측 검증 결과 보고:
- 페어링은 됐는데 MQTT 가 "Not authorized" → mqtt_token 이 잘못 NVS 에 저장/로드. 시뮬레이터로 같은 토큰 시도해서 비교.
- telemetry 가 백엔드에 안 들어옴 → 토픽 오타 또는 payload JSON 포맷 미스. mosquitto_sub -t 'esp32/+/telemetry' -v 로 raw 메시지 확인.
- command 받았는데 처리 안 됨 → cJSON 파싱 실패 또는 action 분기 빠짐. ESP_LOGD 추가.

참조:
  sim_device.py 의 SimDevice._on_message 보면 정확한 처리 패턴 보임.
```
