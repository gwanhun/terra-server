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
  "led_brightness": 75
}
```

- `ts`: SNTP 동기화 시 epoch seconds, 미동기화 시 boot 후 monotonic ms
- 센서 fault 시 `ok: false`, `t/h` 값은 무의미
- `relay` 는 실제 워터펌프 (API 호환성 위해 이름 유지)
- `fan2` 는 냉각팬 (두 번째 팬, 동작은 `fan` 과 동일). 구 펌웨어는 키 없음 → 서버는 NULL 저장
- `led_brightness` 는 MOSFET 보드만 (0~100), 릴레이 보드는 키 없음

카메라 워커 telemetry (15초 주기, heartbeat 성격 — 서버는 `telemetry` 행을 INSERT 하지 않음):

```json
{
  "ts": 1757300000,
  "uptime_sec": 123,
  "free_heap": 456789,
  "rotate_180": false,
  "capabilities": { "rotate_180": true }
}
```

- `rotate_180` (2026-09-08+): 카메라의 현재 NVS 값. 서버가 `cameras.rotate_180`(진실)과 비교해
  다르면 `set_rotation` 재발행. 구 펌웨어는 키 없음 → 동기화 비활성.
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
  - `relay_toggle` / `fan_toggle`
  - `heater_toggle` / `heater_clear`
  - `led_on` / `led_up` / `led_down`
  - `token_rotate` (추가 필드: `new_token`)
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
| 2026-09-08 | 0.5.0 | 카메라 `set_rotation` action, 카메라 telemetry `rotate_180`/`capabilities` (앱 핸드오프 rotate180) |
| 2026-09-08 | 0.5.1 | **ACL 버그 수정**: 카메라 계정에 `telemetry` 쓰기 권한 추가 (누락으로 카메라 heartbeat 가 브로커에서 버려지던 문제). 기존 카메라는 `scripts/regen_acl.py` 로 재생성 |
