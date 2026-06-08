# 펌웨어 통합 가이드 (ESP32-S3)

> **펌웨어 AI 의 단일 진실 소스.** terra-server 백엔드와 어떻게 통신해야 하는지 한 페이지로.
> 참조 구현: [scripts/sim_device.py](../scripts/sim_device.py) — Python 으로 ESP32 흉내 (백엔드 검증용 + 펌웨어 동작 ground truth)

## 0. 인프라 주소 (현재 운영)

| 항목 | 주소 | 포트 |
|------|------|------|
| API 서버 | `https://api.terra-server.uk` | 443 (HTTPS) |
| MQTT 브로커 | `mqtt.terra-server.uk` | 8883 (TLS) |
| TLS 인증서 | Let's Encrypt (시스템 CA store) | — |
| API 인증 | Supabase JWT (Bearer) | — |
| MQTT 인증 | username/password (per-device, 페어링 시 발급) | — |

## 1. 부팅 시퀀스 (state machine)

```
┌─────────────────┐
│   부팅 (boot)   │
└────────┬────────┘
         │
         ▼
   ┌─────────────┐         있음          ┌──────────────────┐
   │ NVS 읽기    │──────────────────────▶│ MQTT 모드 (5장)  │
   │ device_id?  │                       └──────────────────┘
   └──────┬──────┘
          │ 없음
          ▼
   ┌─────────────────────┐
   │ BLE 페어링 모드     │ (2장)
   │ "Terra-XXXX" 광고   │
   └──────┬──────────────┘
          │ 앱이 BLE write { ssid, password, jwt, name }
          ▼
   ┌─────────────────────┐
   │ WiFi 연결           │ (3장)
   └──────┬──────────────┘
          │ 성공
          ▼
   ┌─────────────────────┐
   │ HTTPS POST          │ (4장)
   │ /devices/pair       │
   └──────┬──────────────┘
          │ device_id + mqtt_token 응답
          ▼
   ┌─────────────────────┐
   │ NVS 저장 (영구)     │
   └──────┬──────────────┘
          ▼
   ┌─────────────────────┐
   │ MQTT 모드           │ (5장)
   └─────────────────────┘
```

## 2. BLE 페어링 모드

펌웨어 측은 NimBLE 기반. 자세한 BLE 프로토콜은 [NimBLE_Connection/docs/ble_protocol.md](../../esp32/NimBLE_Connection/docs/ble_protocol.md) 참조.

앱이 전달해야 할 데이터 (JSON or 구분자 분리):
```json
{
  "ssid": "MyWiFi",
  "password": "wifipass",
  "jwt": "eyJhbGc...",
  "name": "거실 비어디다"
}
```

> `jwt` 는 사용자의 Supabase access_token (앱 로그인 후 받음). 4장 HTTPS POST 의 `Authorization: Bearer <jwt>` 헤더에 사용.

### 2.1 NimBLE 펌웨어 텍스트 명령 프로토콜 (참조 구현)

`~/project/esp32/NimBLE_Connection` 의 GATT RX char 가 받는 명령 (텍스트, ASCII):

| 명령 | 동작 |
|------|------|
| `SCAN` | WiFi 스캔 → TX notify 로 결과 |
| `SSID:<ssid>` | SSID 저장 |
| `PASS:<password>` | WiFi password 저장 |
| `NAME:<name>` | 디바이스 이름 저장 (cloud_client_pair 시 서버 전달) |
| `JWT_BEGIN <total_length>` | **JWT chunking 시작.** total_length = JWT 전체 글자 수(십진수). 펌웨어가 그 길이만큼 버퍼 할당. |
| `JWT:<chunk>` | JWT 청크 누적. 청크는 raw substring (base64 아님). **누적 길이 == total_length 도달 시 완성.** |
| `CONNECT` | WiFi 연결 → got_ip 시 NVS 자격증명 없으면 자동으로 `POST /devices/pair` 진행 |

권장 전송 순서: `SSID` → `PASS` → `NAME` → `JWT_BEGIN` → `JWT × N` → `CONNECT`.
청크 크기는 BLE MTU(256) 이내. JWT 약 1000자 가정 시 4~5 청크.
펌웨어 측 JWT 버퍼 상한: 2048 바이트.

## 3. WiFi 연결

`wifi.h` 의 `wifi_connect(ssid, password)` 호출. 연결 실패 시 BLE 페어링 모드 복귀 또는 재시도 (정책 결정).

## 4. HTTPS POST `/devices/pair` (페어링 API)

WiFi 연결 직후 1회 호출. 응답으로 영구 자격증명 받음.

### 요청
```http
POST /devices/pair HTTP/1.1
Host: api.terra-server.uk
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "name": "거실 비어디드",
  "species": "bearded_dragon",       // 옵션
  "firmware_ver": "terra-fw 0.1.0"   // 옵션
}
```

### 응답 (201 Created)
```json
{
  "id": "11111111-2222-3333-4444-555555555555",
  "device_id": "terra-a1b2c3d4",
  "mqtt_token": "Xa2bC9dE..."
}
```

- `device_id`: **MQTT client_id + username** 으로 사용. NVS 저장 필수.
- `mqtt_token`: **MQTT password** 평문. **이 응답에만 1회 노출.** NVS 저장 필수. 분실 시 재페어링.
- `id`: 백엔드 내부 UUID. 펌웨어는 안 써도 됨 (저장은 선택).

### 에러 응답
| HTTP | 의미 | 펌웨어 처리 |
|------|------|------------|
| 401  | JWT 만료/잘못됨 | BLE 페어링 다시 시작 (새 JWT 받기) |
| 422  | name 누락 등 | 앱에 다시 요청 |
| 500/502 | 서버 오류 | 지수 백오프 재시도 (최대 5회) |

### NVS 저장 키 권장
- `device_id` (string, ~30B)
- `mqtt_token` (string, ~50B)
- `owner_uuid` (선택, 디버그용)

## 5. MQTT 모드

### 5.1 연결 설정

| 항목 | 값 |
|------|-----|
| host | `mqtt.terra-server.uk` |
| port | `8883` |
| TLS | enabled (시스템 CA 사용, Let's Encrypt) |
| client_id | `device_id` (e.g. `terra-a1b2c3d4`) |
| username | `device_id` 동일 |
| password | `mqtt_token` (NVS 저장된 평문) |
| keepalive | 60s |
| clean_session | true |

### 5.2 토픽 4개

| 방향 | 토픽 | QoS | retain | 빈도 |
|------|------|-----|--------|------|
| → 서버 | `esp32/{device_id}/telemetry` | 0 | **false** | 3초 |
| → 서버 | `esp32/{device_id}/ack` | 1 | **false** | 명령마다 |
| → 서버 | `esp32/{device_id}/alert` | 1 | **false** | 이상 시 |
| 서버 → | `esp32/{device_id}/command` | 1 | **false** | 사용자 발생 |

> ⚠ **retain=false 절대 변경 X.** 재연결 시 옛 명령이 다시 들어오면 위험 (heater_toggle 등).

### 5.3 페이로드 — Telemetry (3초 publish)

```json
{
  "ts": 1748000000,
  "dht22_a": { "t": 25.3, "h": 62.1, "ok": true },
  "dht22_b": { "t": 24.8, "h": 60.5, "ok": true },
  "relay":  "OFF",
  "fan":    "ON",
  "heater": { "state": "OFF", "locked": false }
}
```

- `ts`: SNTP 동기화 후 epoch seconds. 미동기화 시 boot monotonic ms 도 OK (서버가 처리).
- `dht22_*.ok = false`: 센서 fault. t/h 무시됨.
- `relay`: 워터펌프 (이름 호환성 유지).
- `heater.locked`: safety latch 활성 여부.

### 5.4 페이로드 — Command (subscribe 후 수신)

```json
{
  "msg_id": "uuid-of-command",
  "issued_at": 1748000010,
  "ttl_sec": 10,
  "action": "heater_toggle"
}
```

`action` 종류:

| action | 페이로드 추가 | 펌웨어 동작 |
|--------|--------------|------------|
| `relay_toggle` | — | 워터펌프 GPIO 토글 |
| `fan_toggle` | — | 팬 GPIO 토글 |
| `heater_toggle` | — | 히터 GPIO 토글 (단, latch 활성 시 거부) |
| `heater_clear` | — | safety latch 해제 |
| `led_on` | — | LED 점등 |
| `led_up` | — | LED PWM duty + |
| `led_down` | — | LED PWM duty - |
| `token_rotate` | `new_token` (string) | NVS 의 mqtt_token 갱신 후 MQTT 재연결 |

#### 펌웨어 측 안전망 (필수)
1. **msg_id 중복 제거** — 최근 8개 링버퍼, 같은 msg_id 두 번째 → `rejected_duplicate_msg_id`
2. **TTL 검증** — `now - issued_at > ttl_sec` 이면 폐기 + `rejected_ttl_expired`
3. **heater latch 우선** — latch 활성 시 `heater_toggle` 거부 + `rejected_locked`
4. **알 수 없는 action** — `rejected_unknown_action`

### 5.5 페이로드 — Ack (모든 command 후 publish)

```json
{
  "msg_id": "uuid-of-command",
  "result": "ok",
  "state": { "heater": "ON", "locked": false }   // 옵션
}
```

`result` 값:
- `"ok"`
- `"rejected_locked"`
- `"rejected_ttl_expired"`
- `"rejected_unknown_action"`
- `"rejected_duplicate_msg_id"`

### 5.6 페이로드 — Alert (이상 발생 시 publish)

```json
{
  "kind": "heater_latched",
  "severity": "critical",
  "message": "히터 90초 연속 가동 — safety latch ON",
  "context": { "duration_sec": 90 }
}
```

`kind` (IoT 디바이스):
- `temp_high` / `temp_low` / `humid_low`
- `heater_latched` / `sensor_fault`

> `offline` 은 서버가 last_seen 기반으로 자체 생성 — 펌웨어는 publish 안 함.

## 6. 안전망 (펌웨어 측 필수 구현)

| 항목 | 정책 |
|------|------|
| heater 연속 가동 ≥ N초 | safety latch 활성 (heater OFF + alert publish) |
| 클라우드 명령 단절 | 펌웨어 자체 온/습도 제어 계속 (히스테리시스) |
| MQTT 끊김 | 자동 재연결 (지수 백오프) + telemetry buffer (선택) |
| 명령 TTL 초과 | 폐기 + reject ack |
| msg_id 중복 | 폐기 + reject ack |

## 7. ESP-IDF 컴포넌트 권장

```cmake
PRIV_REQUIRES esp_wifi esp_http_client esp-mqtt mbedtls nvs_flash json
              esp_event esp_netif bt esp_driver_gpio
```

JSON 파싱: `cJSON` (ESP-IDF 내장).

## 8. 테스트 — mosquitto 명령

펌웨어 작성/디버그 시 백엔드와 직접 소통 확인:

### 8.1 telemetry publish 시뮬레이션
```bash
mosquitto_pub -h mqtt.terra-server.uk -p 8883 --capath /etc/ssl/certs \
  -u terra-a1b2c3d4 -P <mqtt_token> \
  -t 'esp32/terra-a1b2c3d4/telemetry' \
  -m '{"ts":1748000000,"dht22_a":{"t":25,"h":60,"ok":true},"dht22_b":{"t":24,"h":58,"ok":true},"relay":"OFF","fan":"ON","heater":{"state":"OFF","locked":false}}'
```

### 8.2 command 수신 시뮬레이션 (대기)
```bash
mosquitto_sub -h mqtt.terra-server.uk -p 8883 --capath /etc/ssl/certs \
  -u terra-a1b2c3d4 -P <mqtt_token> \
  -t 'esp32/terra-a1b2c3d4/command' -v
```

웹에서 명령 발행하면 즉시 한 줄 떠야 함.

### 8.3 ack publish 시뮬레이션
```bash
mosquitto_pub ... \
  -t 'esp32/terra-a1b2c3d4/ack' \
  -m '{"msg_id":"<위에서 받은 msg_id>","result":"ok"}'
```

## 9. 체크리스트 (펌웨어 구현 항목)

### 9.1 페어링 (4장)
- [ ] BLE GATT char: ssid/password/jwt/name 수신
- [ ] `wifi_connect(ssid, password)`
- [ ] `cloud_client_pair(jwt, name)` — HTTPS POST `/devices/pair`
- [ ] cJSON 으로 응답 파싱 → device_id, mqtt_token 추출
- [ ] NVS save: `device_id`, `mqtt_token`

### 9.2 MQTT (5장)
- [ ] NVS load 시도 → 있으면 BLE 스킵
- [ ] `mqtt_app_init(device_id, mqtt_token)` — esp-mqtt with TLS
- [ ] 4 토픽 구독 (`telemetry`/`ack`/`alert` write, `command` read)
- [ ] 3초 주기 telemetry publish
- [ ] command 수신 → JSON 파싱 → msg_id 중복/TTL 검증 → 실행 → ack
- [ ] alert publish (heater_latched 등)

### 9.3 안전망 (6장)
- [ ] heater 연속 가동 카운터 → safety latch
- [ ] 클라우드 명령 단절 시 자체 제어 (히스테리시스)
- [ ] MQTT 재연결 backoff

### 9.4 token_rotate 처리
- [ ] command `token_rotate` 수신 시 NVS 업데이트
- [ ] MQTT 재연결 (새 password)
- [ ] ack 발행

## 10. 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-06-08 | 0.1.0 | 최초 작성 (펌웨어 AI 통합 명세) |

---

## 부록 A. 관련 문서

- [docs/MQTT.md](MQTT.md) — MQTT 페이로드 상세 (백엔드 시각)
- [docs/API.md](API.md) — REST API 전체 명세
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — 시스템 맵
- [scripts/sim_device.py](../scripts/sim_device.py) — Python 참조 구현

## 부록 B. 펌웨어 ↔ 백엔드 일치성 검증

펌웨어 AI 가 작성한 코드가 본 명세와 일치하는지 빠르게 확인:

```bash
# 백엔드 (terra-server) 에서
uv run python scripts/sim_device.py --pair --jwt <jwt>
# → 페어링 + telemetry 3회 publish + command 1회 수신 검증

# 그 다음 실제 ESP32 펌웨어로 같은 동작 → 결과 비교
```
