# MQTT 토픽 / 페이로드 명세

## 브로커

- Mosquitto 2.x on Lightsail
- 포트: **8883 (TLS only)**
- 인증: username/password (per device + bridge 전용 1개)
- Mosquitto ACL: 디바이스는 본인 토픽만, bridge 는 모든 토픽

## 토픽 구조

### IoT (ESP32-S3)

| 토픽 | 방향 | QoS | retain | 빈도 |
|------|------|-----|--------|------|
| `esp32/{device_id}/telemetry` | 디바이스 → 서버 | 0 | false | 3초 |
| `esp32/{device_id}/command` | 서버 → 디바이스 | 1 | **false 필수** | 사용자 발생 |
| `esp32/{device_id}/ack` | 디바이스 → 서버 | 1 | false | 명령마다 |
| `esp32/{device_id}/alert` | 디바이스 → 서버 | 1 | false | 이상 시 |

### 카메라 (ESP32-P4 / RPi 공통)

| 토픽 | 방향 | QoS | retain | 빈도 |
|------|------|-----|--------|------|
| `esp32/{camera_id}/telemetry` | 카메라 → 서버 | 1 | false | 15초 (heartbeat + `rotate_180`/`capabilities`) |
| `esp32/{camera_id}/motion_event` | 카메라 → 서버 | 1 | false | 모션 감지 시 |
| `esp32/{camera_id}/alert` | 카메라 → 서버 | 1 | false | SD 풀/업로드 실패 등 |
| `esp32/{camera_id}/command` | 서버 → 카메라 | 1 | false | 설정 변경, 스트리밍 시작/종료 등 |
| `esp32/{camera_id}/ack` | 카메라 → 서버 | 1 | false | 명령마다 |

> **영상 파일 자체는 MQTT 가 아닌 HTTPS PUT (R2 presigned URL)** 으로 업로드.
> **WebRTC 라이브 스트림**은 MQTT 시그널링 + UDP P2P (혹은 TURN 경유).
> MQTT 는 메타/이벤트/명령 알림 채널.

`device_id` = `devices.device_id` (e.g. `terra-a1b2c3d4`)
`camera_id` = `cameras.camera_id`
- ESP32-P4 워커: `p4cam-a1b2c3d4`
- RPi 워커: `picam-a1b2c3d4`

→ 토픽 prefix 는 `esp32/` 로 통일 (브로커 ACL 단순화. 향후 `terra/` generic prefix 로 변경 검토 가능).

## 페이로드 스키마 (JSON, UTF-8)

### 1. Telemetry

```json
{
  "ts": 1748000000,
  "dht22_a": { "t": 25.3, "h": 62.1, "ok": true },
  "dht22_b": { "t": 24.8, "h": 60.5, "ok": true },
  "relay":  "OFF",
  "fan":    "ON",
  "fan2":   "OFF",
  "heater": { "state": "OFF", "locked": false },
  "led":    "ON",
  "led_brightness": 75,
  "hw_id": "A0B7651C2908"
}
```

- `ts`: SNTP 동기화 시 epoch seconds, 미동기화 시 boot 후 monotonic ms
- 센서 fault 시 `ok: false`, `t/h` 값은 무의미
- `relay` 는 실제 워터펌프 (API 호환성 위해 이름 유지)
- `fan2` 는 냉각팬 (두 번째 팬, 동작은 `fan` 과 동일). 구 펌웨어는 키 없음 → 서버는 NULL 저장
- `led_brightness` 는 MOSFET 보드만 (0~100), 릴레이 보드는 키 없음
- `hw_id` (2026-09-21+): 보드 불변 하드웨어 ID(efuse base MAC 12자리 hex). 페어링에서 이미
  저장되지만, 구 펌웨어로 등록돼 `devices.hw_id` 가 NULL 인 행은 새 펌웨어의 첫 telemetry 에서
  채워진다. 디바이스 telemetry 는 3초 주기라 프로세스당 1회만 UPDATE 한다. 용도는 **이미 생겨버린
  중복 기기 행을 실물 보드와 대조해 정리**하는 것이고, 중복 생성 자체를 막는 것은
  `POST /devices/pair` 쪽이다(API.md 3.2 참고).

카메라 워커 telemetry (15초 주기, heartbeat 성격 — 서버는 `telemetry` 행을 INSERT 하지 않음):

```json
{
  "ts": 1757300000,
  "uptime_sec": 123,
  "free_heap": 456789,
  "reset": "SW:net_wd",
  "hw_id": "30EDA0E22E80",
  "rotate_180": false,
  "capabilities": { "rotate_180": true }
}
```

- `rotate_180` (2026-09-08+): 카메라의 현재 NVS 값. 서버가 `cameras.rotate_180`(진실)과 비교해
  다르면 `set_rotation` 재발행. 구 펌웨어는 키 없음 → 동기화 비활성.
- `clips` (2026-09-16+): 클립 파이프라인 카운터(부팅 후 누적). `rec` 녹화 완료, `skip` 슬롯 없음 스킵(PSRAM 폴백 업로드 중), `skip_lock` H.264 락 핸드오버 실패, `up_ok`/`up_fail` 즉시 업로드, `sd_ok`/`sd_fail` SD 재업로드, `sd_backlog` SD 대기 건수, `last_rec_s` 마지막 녹화 후 초(-1 없음), `up_busy_s` 진행 중 업로드 초(-1 없음). 서버는 `cameras.clip_stats` 에 저장하고 스킵·실패 증가 / 업로드 300초 이상 진행 시 경고 로그.
- `reset` (2026-09-17+): 이번 부팅의 원인. `POWERON` / `PANIC` / `INT_WDT` / `TASK_WDT` / `BROWNOUT` 등
  `esp_reset_reason()` 값, 또는 펌웨어가 스스로 재부팅한 경우 `SW:<why>` (`net_wd` 네트워크 워치독,
  `mqtt_stuck`, `rtc_send_stall`/`rtc_loop_stall`/`rtc_lock_stall` WebRTC 스톨, `cam_stall` 카메라
  DQBUF 무프레임, `rotate`, `ble_reprov`, `http_restart`, `boot:<단계>`). 서버는 `uptime_sec`·`reset`·
  `free_heap` 을 `cameras.clip_stats.sys{uptime_s,reset,heap}` 로 저장하고(별도 컬럼 없음), uptime 이
  직전 heartbeat 보다 줄면 "재부팅 감지 reset=…" 경고 로그. 콘솔 clips 셀에 `up 12m · reset …` 표시.
- `hw_id` (2026-09-21+): 보드 불변 하드웨어 ID(efuse base MAC 12자리 hex). 페어링 때 이미
  저장되지만, 구 펌웨어로 등록돼 `cameras.hw_id` 가 NULL 인 행은 새 펌웨어의 첫 heartbeat 에서
  채워진다(값이 바뀌지 않으므로 1회만 UPDATE — Realtime 잡음 방지). 용도는 **이미 생겨버린
  중복 카메라 행을 실물 보드와 대조해 정리**하는 것. 중복 생성 자체를 막는 것은
  `POST /cameras/pair` 쪽이다(API.md 4.2 참고).
- `img` (2026-09-17+): 노출/야간 상태. `exp`(센서 AE 목표 2~235), `luma`, `chroma`(32px 표본 평균), `night`, `ae_auto`, `ae_frozen`(AE 진동 감지 동결). 서버는 `cameras.image_state` 에 저장, `ae_frozen` 이면 경고 로그.
- `capabilities` (2026-09-08+): 펌웨어 능력 플래그. 서버가 `cameras.capabilities` 에 저장
  (값이 같으면 UPDATE 생략 — 앱이 cameras Realtime 구독 중). MQTT 연결 직후 1회만 실어도 되고
  매번 실어도 된다. 앱은 `capabilities.rotate_180 == true` 일 때만 회전 토글을 노출.

### 2. Command (서버 → 디바이스)

```json
{
  "msg_id": "a1b2c3d4-...",
  "issued_at": 1748000010,
  "ttl_sec": 10,
  "action": "heater_toggle"
}
```

- `msg_id`: commands.id (UUID) 그대로 사용
- `ttl_sec`: 기본 10초. 디바이스는 `now - issued_at > ttl_sec` 이면 폐기
- `action` (IoT 디바이스, ESP32-S3):
  - `relay_on` / `relay_off` / `relay_toggle` — 워터펌프
  - `fan_on` / `fan_off` / `fan_toggle` — 팬 (`*_on` 은 `duration_ms` 옵션: one-shot 자동 OFF, 최대 2h)
  - `fan2_on` / `fan2_off` / `fan2_toggle` — 냉각팬 (동작은 fan 과 동일)
  - `heater_on` / `heater_off` / `heater_toggle` / `heater_clear_lock`
    — ⛔ **펌웨어 미구현**(핸들 NULL). 2026-09-16 부터 **서버가 발행 전에 거절**한다:
      예약은 `POST /schedules` 400, 즉시 명령은 dispatcher 가 `rejected` / `result=unsupported_action`.
      히터 보드가 생기면 `capabilities.heater` 플래그로 다시 연다
  - `led_on` (payload `brightness` 0~100 옵션, MOSFET 조광) / `led_off` / `led_toggle`
    — ⚠️ **`duration_ms` 미지원.** 보내면 **무시하고 켠 뒤 `ok` 응답**(자동 OFF 없음, 실패 감지 불가)
    — ⚠️ `brightness: 0` 은 `led_on` 이어도 실제로 꺼지고 `state: "OFF"` 응답
  - `mist` (`duration_ms`: 1000|2000|3000, 펌웨어 상한 5초) / `spray_1s` / `spray_3s` / `spray_5s`
  - `lcd_bitmap` / `lcd_clear`
  - `set_temp_offset` (`offset_c`: -10.0~10.0) — 온도 보정 오프셋. 기기가 NVS(`terra/t_off`)에 저장하고
    센서 읽기 직후 적용해 **LCD·telemetry·HTTP 가 모두 같은 보정값**을 쓴다. ack `state="CALIB"`,
    범위 밖/숫자 아님은 `result="bad_request"`. 구 펌웨어는 `unknown_action`.
  - `token_rotate` (추가 필드: `new_token`)

> **`duration_ms` 를 실제로 처리하는 action 은 `mist` / `fan_on` / `fan2_on` 셋뿐이다**
> (펌웨어 실측 2026-09-16). 나머지는 조용히 무시되고 `ok` 가 돌아온다.
> 보드 차이: 릴레이 보드(`terra-iot-nano-relay`)에는 **`fan2_*` 코드가 없고**(→ `unknown_action`),
> `led_on` 의 `brightness` 도 파싱하지 않는다. 상세: [BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md](BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md)
>
> **payload 예약 키**: `msg_id` / `issued_at` / `ttl_sec` / `action` 은 서버가 정한다.
> `commands.payload` 에 같은 키가 있어도 서버가 무시한다(명령 바꿔치기 차단).
- `action` (카메라 워커, ESP32-P4 / RPi):
  - `snapshot_stream` (추가: `interval_ms`, `duration_sec`) — Stage G1
  - `snapshot_stop` — Stage G1
  - `webrtc_offer` (추가: `sdp`, `session_id`) — Stage G2
  - `webrtc_ice` (추가: `candidate`, `session_id`) — Stage G2
  - `webrtc_close` (추가: `session_id`) — Stage G2
  - `set_rotation` (추가: `rotate_180: bool`) — 영상 180° 회전(설치 방향 보정). `ttl_sec` 60.
    서버가 `PATCH /cameras/{id}` 직후 발행하고, 카메라 telemetry 의 `rotate_180` 이 DB 와
    다르면 브리지가 재발행(카메라당 최소 60초 간격). 펌웨어는 vflip+hmirror 동시 적용 후
    NVS 저장, ack `"ok"`. 구 펌웨어는 `rejected_unknown_action`.
  - `token_rotate`

### 3. Ack

```json
{
  "msg_id": "a1b2c3d4-...",
  "result": "ok",
  "state": { "heater": "ON", "locked": false }
}
```

`result` 값:
- `"ok"`
- `"rejected_locked"` — 히터 latch 활성
- `"rejected_ttl_expired"` — TTL 초과
- `"rejected_unknown_action"`
- `"rejected_duplicate_msg_id"`

### 4. Alert

```json
{
  "kind": "temp_high",
  "severity": "warning",
  "message": "DHT22-A 온도 45.2°C 초과",
  "context": {
    "t_a": 45.2,
    "threshold": 45.0
  }
}
```

`kind` 값:
- IoT: `temp_high` / `temp_low` / `humid_low` / `heater_latched` / `sensor_fault`
- 카메라: `sd_full` / `r2_upload_failed` / `camera_fault`
- (`offline` 은 bridge 가 last_seen 기반으로 자체 생성)

### 5. Motion Event (카메라 → 서버)

```json
{
  "ts": 1748000000,
  "motion_score": 0.42,
  "planned_size": 1048576,
  "planned_duration_sec": 10.0,
  "resolution": "HD",
  "fps": 24,
  "codec": "h264"
}
```

- 모션 감지 직후 publish (영상 캡처 시작 알림용)
- 실제 영상 업로드는 별도 HTTPS POST + R2 PUT 흐름
- bridge 는 motion_event 를 로깅만 (선택적으로 alerts INSERT for "motion_detected")
- HD 720p 10초 H.264 기준 파일 크기 약 500KB~1.5MB

## 명령 안전성

1. **msg_id 중복 제거** — 디바이스는 최근 처리한 8개 msg_id 링버퍼 유지
2. **TTL 검증** — `issued_at` + `ttl_sec` 로 stale 명령 폐기
3. **retain=false 강제** — bridge 코드에 하드코딩, 절대 변경 금지
4. **물리 안전망 독립** — heater_check_temp 가 클라우드와 무관하게 3초마다 평가

## Mosquitto ACL 예시

```
# /etc/mosquitto/acl

# 브리지 (모든 토픽)
user terra-bridge
topic readwrite esp32/#

# IoT 디바이스
user terra-a1b2c3d4
topic write esp32/terra-a1b2c3d4/telemetry
topic write esp32/terra-a1b2c3d4/ack
topic write esp32/terra-a1b2c3d4/alert
topic read  esp32/terra-a1b2c3d4/command

# 카메라 (ESP32-P4 워커)
user p4cam-a1b2c3d4
topic write esp32/p4cam-a1b2c3d4/telemetry
topic write esp32/p4cam-a1b2c3d4/motion_event
topic write esp32/p4cam-a1b2c3d4/alert
topic write esp32/p4cam-a1b2c3d4/ack
topic read  esp32/p4cam-a1b2c3d4/command

# 카메라 (RPi 워커, 대안)
user picam-b2c3d4e5
topic write esp32/picam-b2c3d4e5/telemetry
topic write esp32/picam-b2c3d4e5/motion_event
topic write esp32/picam-b2c3d4e5/alert
topic write esp32/picam-b2c3d4e5/ack
topic read  esp32/picam-b2c3d4e5/command
```

(ACL 자동 생성 스크립트는 추후 `scripts/regenerate_mosquitto_acl.py` 로 작성)

## 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-05-26 | 0.1.0 | 최초 명세 |
| 2026-05-26 | 0.2.0 | ESP32-CAM 토픽 추가 (motion_event), ACL 분리 |
| 2026-05-27 | 0.3.0 | 카메라 하드웨어 RPi Zero 2 W 로 변경 (H.264, mp4) |
| 2026-05-27 | 0.4.0 | 메인 카메라 워커 ESP32-P4 로 변경, Stage G(라이브 스트리밍) action 추가 |
| 2026-09-07 | 0.4.1 | telemetry `fan2`(냉각팬) 추가, IoT action 목록 현행화 (`fan2_*`, on/off 계열, mist/lcd) |
| 2026-09-08 | 0.5.0 | 카메라 `set_rotation` action, 카메라 telemetry `rotate_180`/`capabilities` (앱 핸드오프 rotate180) |
| 2026-09-16 | 0.6.0 | 카메라 telemetry `clips` 카운터 → `cameras.clip_stats` (조용한 정지 감시) |
| 2026-09-08 | 0.5.1 | **ACL 버그 수정**: 카메라 계정에 `telemetry` 쓰기 권한 추가 (누락으로 카메라 heartbeat 가 브로커에서 버려지던 문제). 기존 카메라는 `scripts/regen_acl.py` 로 재생성 |
| 2026-09-16 | 0.5.2 | 펌웨어 실측 반영: `led_on` 의 `duration_ms` 미지원·`heater_*` 미구현 명시, payload 예약 키 보호 |
| 2026-09-16 | 0.5.3 | `heater_*` 서버 거절(400 / `unsupported_action`), 소프트 해제된 기기의 메시지는 브리지가 미페어링 취급 |
| 2026-09-20 | 0.5.4 | IoT `set_temp_offset` action 추가 (온도 보정, NVS 영속, LCD/telemetry 공통 적용) |
