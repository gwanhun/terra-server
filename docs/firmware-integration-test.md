# ESP32-S3 펌웨어 ↔ terra-server 통합 테스트 (Phase 1)

> Stage A 의 telemetry 도착 검증. 명령 수신/ack/alert 는 Phase 2/3 에서.
> 펌웨어 레포: `~/project/esp32/NimBLE_Connection`

## Phase 분할

| Phase | 검증 대상 | 펌웨어 작업 | 백엔드 작업 |
|-------|----------|------------|-----------|
| **1** | telemetry → DB → 웹 실시간 차트 | payload 스펙 매칭 (완료) | manual device INSERT |
| 2 | command → 액추에이터 → ack → status='acked' | MQTT_EVENT_DATA 핸들러 확장 | 없음 |
| 3 | heater latch 등 → alert 패널 즉시 표시 | alert publish 추가 | 없음 |

## Phase 1 — telemetry 도착 확인

### 0. 사전 점검

- Lightsail 인스턴스 가동, terra-api / terra-bridge / mosquitto systemd 정상 (`systemctl status`)
- Supabase 마이그레이션 적용 완료 (`devices`, `telemetry` 테이블 존재)
- TLS: `mqtt.terra-server.uk` / `api.terra-server.uk` 인증서 발급됨
- 웹 콘솔에서 사용자 계정 로그인 가능 (회원가입 완료)

### 1. Mosquitto 공용 디바이스 계정 등록 (Stage A 한정)

Stage A 는 모든 디바이스가 공용 계정 `terra-device` 사용. Stage B (per-device 페어링) 도입 전 임시 방식.

```bash
ssh ubuntu@<lightsail-ip>

# 공용 계정 생성 (-c 는 새 파일, 두 번째부터는 -c 빼기)
sudo mosquitto_passwd -c /etc/mosquitto/passwd terra-bridge   # 이미 있으면 스킵
sudo mosquitto_passwd /etc/mosquitto/passwd terra-device
# 강한 비번 입력 → 메모

# ACL 갱신
sudo tee -a /etc/mosquitto/acl > /dev/null <<'EOF'

user terra-device
topic readwrite esp32/#
EOF

sudo systemctl restart mosquitto
```

> Stage B 부터는 `terra-{MAC}` username 별로 ACL 자동 추가하는 스크립트로 전환.

### 2. 펌웨어 Kconfig 입력 (menuconfig)

```bash
cd ~/project/esp32/NimBLE_Connection
idf.py menuconfig
```

`Example Configuration` 메뉴:

| 키 | 값 |
|---|---|
| WiFi SSID | (집 WiFi SSID) |
| WiFi Password | (집 WiFi 비번) |
| Use static IP | n (DHCP 권장) |
| MQTT broker hostname | `mqtt.terra-server.uk` |
| MQTT broker port (TLS) | `8883` |
| MQTT username | `terra-device` |
| MQTT password | (위에서 mosquitto_passwd 로 등록한 비번) |

저장 후 종료.

추가로 `sdkconfig` 에서 다음 확인 (보통 기본 ON):
- `CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`
- `CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_DEFAULT_FULL=y` (Let's Encrypt ISRG X1 포함)
- `CONFIG_LWIP_SNTP_MAX_SERVERS` ≥ 1 (시간 동기화)

### 3. 빌드 + 플래시 + 시리얼 모니터

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/cu.usbmodem* flash monitor
```

부팅 로그에서 다음 확인:

```
I (xxxx) wifi:connected with <SSID>, aid = ...
I (xxxx) wifi:got ip:192.168.0.xx
I (xxxx) sntp: time synced: ...     <-- 중요 (TLS 검증에 필수)
I (xxxx) mqtt_app: device_id=terra-A1B2C3D4E5F6   <-- 이 값 메모!
I (xxxx) mqtt_app: MQTT client started → mqtt.terra-server.uk:8883
I (xxxx) mqtt_app: MQTT CONNECTED → publish:esp32/terra-A1B2C3D4E5F6/telemetry
I (xxxx) mqtt_app: subscribed → esp32/terra-A1B2C3D4E5F6/command (QoS 1)
```

→ 여기까지 나오면 TLS + 인증 성공.

### 4. Supabase 에 디바이스 수동 등록

펌웨어 측 `device_id` 는 MAC 기반 자동 생성이라 `POST /devices/pair` 응답의 랜덤 ID와 다름. Stage A 는 **Supabase 대시보드에서 직접 INSERT**:

```sql
-- Supabase 대시보드 > SQL Editor

-- 본인 user_id 확인 (auth.users)
SELECT id, email FROM auth.users;

-- 디바이스 등록 (token_hash 는 bcrypt 형식이지만 Stage A 는 검증 안 함 → 더미 OK)
INSERT INTO public.devices (owner_id, device_id, token_hash, name, species)
VALUES (
  '<위에서 복사한 user_id>',
  'terra-A1B2C3D4E5F6',       -- 시리얼에서 본 device_id
  '$2b$12$dummy.hash.stage.a.skip.verification.placeholder',
  '거실 비어디드',
  'bearded_dragon'
);

-- (선택) 임계값 설정 — alerts 검증용
INSERT INTO public.device_settings (device_id, alert_temp_high, alert_temp_low, alert_humid_low)
SELECT id, 45.0, 15.0, 20.0 FROM public.devices WHERE device_id = 'terra-A1B2C3D4E5F6';
```

### 5. 동작 확인

#### A. DB 직접 확인

```sql
SELECT device_id, ts, t_a, h_a, a_ok, relay, fan, heater_state, heater_locked
FROM public.telemetry t
JOIN public.devices d ON d.id = t.device_id
WHERE d.device_id = 'terra-A1B2C3D4E5F6'
ORDER BY ts DESC
LIMIT 10;
```

→ 3초 주기로 row 추가되어야 함. **현재 펌웨어는 DHT22 비활성** (`#if 0`) 이라 `t_a/h_a` 는 0.0, `a_ok=false`. 액추에이터 상태는 정상 publish.

#### B. devices.last_seen_at 자동 갱신 확인

```sql
SELECT device_id, last_seen_at, is_online
FROM public.devices WHERE device_id = 'terra-A1B2C3D4E5F6';
```

→ `is_online=true`, `last_seen_at` 이 최근 3초 내.

#### C. 웹 콘솔 실시간 표시

1. `python3 -m http.server -d web 5500` 로 웹 띄움
2. 브라우저 로그인 → 대시보드 진입
3. **📊 실시간 텔레메트리** 패널에 `terra-A1B2C3D4E5F6` 행 자동 추가 + 3초마다 갱신 확인
4. 우상단 `● 실시간` 표시 = Supabase Realtime 연결됨

#### D. terra-bridge 로그

```bash
ssh ubuntu@<lightsail-ip>
sudo journalctl -u terra-bridge -f
```

→ `telemetry from terra-A1B2C3D4E5F6: {...}` debug 로그 또는 정상 INSERT.

#### E. alerts 검증 (옵션)

펌웨어 측 DHT22 가 비활성이라 `a_ok=false` → 매 telemetry 마다 `sensor_fault` alert 생성됨 (단 1회, dedup 으로). 정상 동작 확인용.

```sql
SELECT kind, severity, message, triggered_at, resolved_at
FROM public.alerts
WHERE device_id = (SELECT id FROM devices WHERE device_id = 'terra-A1B2C3D4E5F6')
ORDER BY triggered_at DESC;
```

→ `sensor_fault` 1건 활성 (resolved_at IS NULL) 확인. 웹 **🚨 활성 알림** 패널에도 표시.

### 6. 트러블슈팅

| 증상 | 원인 / 해결 |
|------|------------|
| `mqtt_app: MQTT ERROR ... MBEDTLS_ERR_X509_CERT_VERIFY_FAILED` | SNTP 미동기화 → 인증서 유효기간 검증 실패. `idf.py menuconfig` 에서 SNTP 활성 확인. 인터넷 안 됨이면 NTP 서버 도달 X. |
| `MQTT ERROR ... auth failed` (CONNACK reason 5) | `terra-device` 비번 불일치 → `sudo mosquitto_passwd /etc/mosquitto/passwd terra-device` 재설정 후 mosquitto restart. |
| 시리얼은 정상인데 DB 에 telemetry 없음 | (1) terra-bridge 미가동 → `systemctl status terra-bridge`. (2) 디바이스 미등록 → 시리얼 device_id 가 정확히 SQL INSERT 값과 일치하는지 (대소문자 hex). bridge 로그에 "미페어링 device_id 무시" 메시지 확인. |
| DB 에는 row 있는데 t_a 가 NULL/0 | 정상 (DHT22 미연결). 펌웨어 `main.c` 의 `#if 0` 풀면 실측값. |
| 웹 실시간 패널이 갱신 안 됨 | (1) `● 실시간` 표시 안 됨 → Supabase publication 에 telemetry 포함됐는지 (initial_schema.sql 의 ALTER PUBLICATION 라인). (2) RLS — 본인 디바이스가 아니어서 SELECT 차단. `devices.owner_id` 가 로그인 user_id 와 일치하는지. |
| `MQTT ERROR esp_tls=0x800x` 류 | TLS handshake 실패. `mqtt.terra-server.uk` DNS 해석 OK인지 (`ping`), Mosquitto 8883 정상 listen 인지 (`sudo ss -tlnp \| grep 8883`). |

### 7. Phase 1 완료 기준

- [ ] 시리얼에서 `MQTT CONNECTED` 로그
- [ ] DB `telemetry` 테이블에 3초 주기로 row 추가
- [ ] `devices.last_seen_at` 자동 갱신, `is_online=true`
- [ ] 웹 **실시간 텔레메트리** 패널에 디바이스 행 표시 + 자동 갱신
- [ ] (옵션) `sensor_fault` alert 활성 알림 패널에 표시

전부 OK 면 Phase 2 (명령 수신 → 액추에이터 → ack) 진행.

---

## Phase 2 — 명령 수신 → 실행 → ack (다음 단계)

### 펌웨어 작업 요약 (구현 예정)

`mqtt_app.c` MQTT_EVENT_DATA 핸들러 확장:

1. payload 를 cJSON 또는 strstr 로 파싱 → `msg_id`, `action`, `issued_at`, `ttl_sec` 추출
2. TTL 검증: `time(NULL) - issued_at > ttl_sec` 이면 `rejected_ttl_expired` ack
3. msg_id 중복 검사 (최근 처리 8개 링버퍼)
4. action 매핑:
   - `relay_toggle` → `relay_toggle(s_relay)`
   - `fan_toggle` → `relay_toggle(s_fan)`
   - `heater_toggle` → `heater_toggle(s_heater)` (latch 검사 결과로 result 다름)
   - `heater_clear` → `heater_clear_lock(s_heater)`
   - `led_on/up/down` → `led_ctrl_pulse_*()`
5. `esp32/{device_id}/ack` publish `{"msg_id":"...","result":"ok|rejected_*","state":{...}}`

→ s_relay/s_fan/s_heater/s_led_ctrl 핸들은 이미 `main.c` 에 전역. `extern` 또는 mqtt_app 에 setter 추가하면 됨.

### 검증 방법

웹 콘솔 → 디바이스 행에서 명령 선택 (heater_toggle 등) → 발행 버튼.
- 시리얼: `COMMAND received: ... heater_toggle` + 액추에이터 동작 로그
- 웹 **최근 명령** 패널: pending → sent (수백 ms) → acked
- 물리 액추에이터 실제 토글

---

## Phase 3 — alert publish (다음 다음)

펌웨어 측 heater latch / sensor fault 감지 시:

```c
mqtt_app_publish_alert("heater_latched", "critical",
                       "히터 안전 latch 발동",
                       /*context*/ "{\"max_temp\":50.0}");
```

→ `handle_alert` 가 `alerts INSERT` → 웹 활성 알림 패널 즉시 표시 (Realtime).
