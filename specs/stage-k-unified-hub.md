# Stage K — Terra Hub: 카메라 보드 한 대로 사육장 전부 운영

**상태**: 🚧 K-1 펌웨어 포팅 ✅(빌드 통과, 실기 미검증) / K-1 서버 ✅(테스트 통과, 마이그레이션 미적용) / K-2 이후 미착수 (2026-10-06)
**펌웨어**: `~/project/esp32/terra-hub-p4` (신규 폴더. `firebeetle2-p4-yr030` 작업트리 복사 + `main/iot/`)
**목표**: 사육장 1곳 = 보드 1대. ESP32-P4 카메라 보드가 센서(DHT22)·펌프·팬·조명까지 맡아 ESP32-S3 기기 보드를 없앤다.

## 왜

| 지금 (보드 2대) | 허브 (보드 1대) |
|---|---|
| 카메라(P4) + 기기(S3) 각각 전원·WiFi·BLE 페어링·펌웨어 | 전원 1, WiFi 1, 페어링 1, 펌웨어 1 |
| "기기는 살고 카메라만 죽는" 류의 반쪽 장애 (BACKLOG §1) | 한 보드 → 한 번에 살거나 죽음. 진단 단순 |
| 사육장 ↔ 카메라/기기 매칭 UI 필요 (`ENCLOSURE_MATCHING_API.md`) | 자동으로 한 쌍 |
| 기기 펌웨어 트리 2개(nano/supermini) 동기화 부담 ([[project-firmware-trees-synced]]) | 트리 1개 |
| 원가 ~$80~110 | ~$50~60 + MOSFET 보드 |

P4 는 듀얼코어 360MHz·PSRAM 32MB 라 센서 폴링과 PWM 4채널은 부담이 아니다. 걸림돌은 **핀 수**와 **서버·앱이 devices/cameras 두 테이블을 전제로 짜여 있다는 점**이다.

## 확정된 설계 결정

### D1. 서버는 "한 보드 = 두 행" — 테이블 합치지 않는다
앱/웹/RLS/Realtime/예약(schedules)/알림(alerts)/푸시가 전부 `devices` 를 FK 로 쓴다. 테이블을 합치면 앱팀 포함 전면 재작업.
대신 허브 1대 = `cameras` 행 1 + `devices` 행 1 로 두고, 두 행이 **같은 텍스트 id(`p4hub-xxxxxxxx`)와 같은 MQTT 토큰 해시**를 가진다. `cameras.device_id` 로 링크.
→ 앱은 기존처럼 "카메라 1 + 기기 1 이 같은 사육장에 있다" 로 본다. **앱 변경 0.**

### D2. MQTT 는 한 계정·같은 토픽, payload 모양으로 분기
- 3초: IoT telemetry (`dht22_a`… 기기 스키마 그대로, QoS 0) → 브리지 **devices 경로** (telemetry INSERT·알림·예약 복원·sys_state·temp_offset)
- 15초: 카메라 heartbeat (기존 그대로, QoS 1) → 브리지 **cameras 경로**
- 판별: `dht22_a` 또는 `dht22_b` 가 dict 이면 센서 payload. (`handlers._resolve_both` + `handle_telemetry`)
- ack: `commands` 에 msg_id 가 있으면 기기 명령 ack, 없으면 카메라 명령(reboot/set_rotation/webrtc_*)의 ack. 허브에선 "매칭 없음" 경고를 내지 않는다.
- alert: 허브의 카메라 알림(`sd_full` 등)도 `alerts` 에 들어간다 (devices 행이 있으니). 보너스.
- ACL: 카메라 블록(telemetry/motion_event/ack/alert write + command read)이 상위집합 → 한 블록만.

왜 두 스트림을 합치지 않나: 합치면 heartbeat 의 큰 payload(errs 최대 1.4KB)를 3초마다 보내고 cameras UPDATE 가 5배 늘어난다. 분리하면 서버 코드 변경이 "분기 한 줄" 로 끝난다.

### D3. 페어링은 `/cameras/pair` 한 번, `model="esp32-p4-hub"`
- 접두사 `p4hub-`. 응답에 `device_uuid` 추가. `capabilities` 요청 필드 추가(→ `devices.capabilities`, `hub:true` 강제).
- 재페어링(hw_id 재사용): cameras 행 재사용 + 링크된 devices 행 **토큰만 갱신**. 새 행 없음.
- BLE 광고 이름은 당장 `FB2_P4_CAM_…` 유지 → 앱은 "카메라 등록" 흐름 그대로 쓰고 서버가 기기 행까지 만든다. 이름 변경은 앱팀 합의 후(K-3).

### D4. 펌웨어는 카메라 트리가 베이스, IoT 는 `main/iot/` 모듈
카메라(1.3만 줄, WebRTC·클립·SD·BLE·워치독)가 어려운 쪽. supermini 에서 가져오는 건 DHT22·relay(펄스/소프트스타트)·light_pwm·calib·command 분기 ≈ 1천 줄.
- `app_mqtt` 에 `on_iot_command` 폴백 + `app_mqtt_publish_telemetry_json` 추가. 카메라 action 을 먼저 보고 모르는 것만 IoT 로.
- 기기 ack 형식 `{msg_id,result,state,ts}` 유지(서버 handle_ack 는 msg_id 만 본다).
- `CONFIG_APP_ENABLE_IOT` 로 게이트 → 끄면 순수 카메라 빌드와 동일(모델 `esp32-p4`).
- **LCD 포함**(2026-10-06 오후 추가): ST7735 화면·`lcd_bitmap`/`lcd_clear`·NVS 밴드를 `main/iot/iot_lcd*` 로 이식. `capabilities.lcd` 로 보고(`APP_IOT_LCD_ENABLE` 끄면 false → 콘솔이 LCD 패널을 숨길 근거).
- **안 가져온 것**: 히터(서버도 거절), 물리 버튼, DS18B20, 기기 HTTP 서버/웹UI.

### D5. 핀 배치 — 2026-10-06 확정 (DFRobot IO 확장보드 회로도 DFR1237 V1.0 으로 헤더 검증)
헤더 실제 배열: **오른쪽** `… 51/A4 · 23/A3 · 22/A2 · 21/A1 · 20/A0 · 36 · 35(BOOT) · 34 · 31`,
**왼쪽** `28/SCK · 29/MO · 30/MI · 8/SCL · 7/SDA · 48 · 49 · 50 · 52 · 4 · 5 · 37/TX · 38/RX`.
- **DHT22** 왼쪽 헤더 **38/RX**(LCD 블록 `…4·5` 바로 다음 칸, 핀 헤더에 직접 꽂음). 그러려고 앱 콘솔을 UART0 → USB-Serial-JTAG 로 옮김(`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, 모니터는 원래 USB-C). 37/TX 는 ROM 부팅 로그가 나와 비워둠. 7/8 은 카메라 I2C 라 불가. DHT22-B 없음(-1, 달면 30)
- **MOSFET 4채널** 오른쪽 헤더 연속 4칸, 보드 채널 순서대로: **조명(PWM1) 20 · 펌프(PWM2) 21 · 팬(PWM3) 22 · 팬2(PWM4) 23**
- **LCD(ST7735, SPI3)** 왼쪽 — 모듈 핀 순서(BLK·CS·DC·RST·SDA·SCL)를 헤더 연속 6칸에 그대로: **BLK 48 · CS 49 · DC 50 · RST 52 · SDA 4 · SCL 5**(리본 직결, 뒤집으면 역순). SPI 는 GPIO 매트릭스라 SPI 라벨 핀이 아니어도 20MHz 문제없음. SPI2 는 SD 카드.
- 남는 핀: 28, 29, 30, 51, 31, 32, 33, 36 (34/35 는 스트래핑·BOOT, 37 은 ROM TX 라 비워둠).
- 7/8 은 카메라 SCCB(I2C) 버스가 헤더로 나온 것 — GPIO 로 쓰면 카메라 제어가 깨진다. I2C 장치만 공유 가능.
절대 피할 핀: **24/25 = USB-Serial-JTAG D-/D+** (첫 가안이 24/25 를 펌프/팬에 썼었다 — 물렸으면 플래시·모니터 불능),
7/8(카메라 SCCB I2C), 14~19(C6 SDIO), 54(C6 리셋), 39/42/43/44(SD SPI), 45(SD 전원), 34~38(부팅 스트래핑·UART0), MIPI 전용.
sdkconfig 의 `BT_NIMBLE_HCI_UART_CTS_PIN=23` 은 UART 전송이 꺼져 있어(VHCI over esp-hosted) 무효 — 23 은 비어 있다.
`-1` 로 끈 채널의 action 은 `unknown_action`.

## In (선행 조건)
- `migrations/2026-10-06_hub_link.sql` 적용 (코드 배포 전) → `MIGRATIONS_APPLIED.md`
- MOSFET 드라이버 보드(all-MOSFET 정책, [[project-iot-board-all-mosfet]]) + P4 헤더 배선
- ESP-IDF v6.0.1, `IDF_PYTHON_ENV_PATH=$HOME/.espressif/tools/python/v6.0.1/venv`
- OTA(Stage J) 와 파티션 충돌 주의: 허브 빌드 1.85MB, factory 1900K 에 **5% 여유**. 16MB 전환·ota_0/1 은 Stage J 가 하고, 허브는 그 파티션 테이블을 그대로 받는다.

## Out
- 테이블 통합(devices+cameras) — 하지 않음(D1)
- 앱 변경 — K-1 에선 0. BLE 이름/등록 UX 는 K-3
- 히터·LCD·버튼 — 허브 범위 외(D4)
- 기존 S3 기기 베타 14대 교체 — 운영 결정, 이 스펙 밖

## 완료 조건

### K-1 — 펌웨어 포팅 + 서버 분기 (이번 세션)
- [x] `~/project/esp32/terra-hub-p4` 생성 (카메라 작업트리 복사, `build/`·`sdkconfig`·`.git` 제외, `git init`)
- [x] `main/iot/` — `iot_dht22` `iot_relay` `iot_light_pwm` `iot_calib` `iot_board_caps` `iot_app` (supermini 이식)
- [x] `main/iot/iot_lcd*` — ST7735 + 커스텀 밴드 + 에셋 (supermini 이식), `lcd_bitmap`/`lcd_clear` 폴백, 센서 태스크에서 그리기 직렬화
- [x] `app_mqtt`: `on_iot_command` 폴백 · `publish_ack_state` · `app_mqtt_publish_telemetry_json`
- [x] `main.c`: `iot_app_init`(NVS 직후, 액추에이터 OFF) → MQTT 시작 후 `iot_app_start` · 페어링 `model=esp32-p4-hub` + `capabilities`
- [x] Kconfig `APP_ENABLE_IOT` + 핀/펌프/보정 옵션, CMake, `sdkconfig.defaults` 에 IOT=y, 프로젝트명 `terra_hub_p4`
- [x] **빌드 통과** (`idf.py build`, 1,849,920 B)
- [x] 서버: `_ALLOWED_MODELS` 에 `esp32-p4-hub`, `_ensure_hub_device`, 응답 `device_uuid`, `cameras.device_id` 마이그레이션
- [x] 서버: `handlers._resolve_both` + telemetry/ack 분기, `registry` ACL 중복 제거
- [x] 테스트 `tests/test_hub.py` 12건 + 기존 396건 통과 (p4cam→p4hub 전환 포함)
- [ ] 마이그레이션 적용 + 프로덕션 배포 (사용자)
- [x] 헤더 핀맵 회로도 검증 (R10) — 20~23/51, 28/29/48~52 헤더 전용
- [ ] **실기 검증**: DHT22 읽힘 · `mist`/`led_on brightness`/`fan_on duration_ms` ack · 콘솔에 기기 행+카메라 행 둘 다 온라인 · 라이브(WebRTC) 중 센서 telemetry 끊김 없음
- [ ] DHT22 크리티컬 섹션(~5ms)이 카메라 DQBUF/인코더에 영향 없는지 `clips.enc_err` 로 확인

### K-2 — 수명주기 묶기
- [ ] `unlink`/`DELETE /cameras/{id}` 가 링크된 devices 행도 함께 해제/삭제 (`unlink_service`, `cameras.device_id`)
- [ ] `offline_monitor`: 허브는 두 행이 각자 last_seen 갱신되므로 기본 동작으로 OK — 단 센서 스트림만 죽은 경우(기기 offline 알림만) 가 허브 장애인지 구분하는 로그
- [ ] 웹 콘솔: 허브 행 뱃지(`capabilities.hub`) + 카메라/기기 행 묶어 표시
- [ ] `docs/FIRMWARE_INTEGRATION.md` 허브 섹션, `docs/API.md` `/cameras/pair` 변경

### K-3 — 앱/UX (앱팀 합의 필요)
- [ ] BLE 광고 이름 `TERRA_HUB_…` 로 — 앱 스캔 접두사 추가
- [ ] 등록 UX: "허브 1개 등록" 이 카메라+기기 둘 다 만든다는 안내. `PAIR_OK` 응답에 `device_uuid` 노출 여부
- [ ] `ENCLOSURE_MATCHING_API.md` 카운트(camera_count/device_count) 허브 중복 표시 정리

## 리스크 / 예상 문제 (2026-10-06 정리)

| # | 리스크 | 영향 | 대응 |
|---|---|---|---|
| R1 | **펌프 돌입전류 → P4 브라운아웃.** 허브는 카메라+C6 WiFi(최대 ≈20dBm, 기기 S3 의 8.5dBm 보다 훨씬 큼)+펌프가 한 보드. 리셋되면 클립·라이브가 같이 끊긴다 | 높음 | 12V 는 별도 어댑터(GND 만 공유), P4 는 5V/3A. 소프트스타트 200ms·run duty 60% 기본. 실측 후 `APP_IOT_PUMP_*` 조정. `reset=BROWNOUT` 이 heartbeat 에 뜨는지 감시 |
| R2 | **부팅 직후 ~1~2초 게이트 플로팅.** 부트로더~`iot_app_init` 전까지 GPIO 는 입력 상태 → MOSFET 보드 입력이 떠 있으면 펌프가 잠깐 돌 수 있다 | 중 | MOSFET 보드 게이트에 풀다운(10kΩ) 확인. 펌웨어는 OFF 상태를 `gpio_hold` 로 묶어 WDT/브라운아웃 리셋 중엔 유지 |
| R3 | **DHT22 크리티컬 섹션(~5ms, 인터럽트 차단)이 카메라 파이프라인(CSI DMA·ISP·H.264 ISR)과 같은 코어에서 돌면** 프레임 드롭·`enc_err`·심하면 `cam_stall` 재부팅 | 중 | 실기에서 `clips.enc_err/enc_streak`·`cam_stall` 추적. 문제면 ① 센서 태스크를 카메라 ISR 반대 코어에 고정 ② RMT RX 로 DHT 엣지 캡처(크리티컬 섹션 0) 로 교체 |
| R4 | **파티션 여유 5%** (1.85MB / 1900K). 로그 한 줄만 늘어도 넘칠 수 있음 | 높음(진행 차단) | Stage J(OTA) 의 16MB·ota_0/1 파티션으로 전환. 그 전까진 기능 추가 금지, 필요하면 `APP_CLIP_STREAM`·웹UI 같은 비활성 코드 제거 |
| R5 | **이미 `p4cam-` 으로 등록된 보드를 허브로 리플래시** → hw_id 가 같아 행 재사용 → id 가 `p4cam-` 그대로면 브리지가 devices 를 안 봐서 센서 telemetry 가 조용히 버려짐 | 높음 | **해결함**: 허브 페어링 시 `camera_id` 를 `p4hub-` 로 재발급 + 옛 MQTT 계정 해지 (`test_pair_hub_on_board_registered_as_plain_camera_renames_to_p4hub`). 펌웨어는 응답의 새 id/token 을 NVS 에 저장 |
| R6 | **한쪽 행만 삭제/해제** (K-2 전): 카메라 행만 지우면 MQTT 계정이 해지돼 허브 전체가 끊김. 기기 행만 지우면 센서 payload 가 카메라 경로로 빠져 cameras.last_seen 만 3초마다 갱신(데이터 유실, 조용함) | 중 | K-2 에서 `unlink`/`DELETE` 를 링크로 묶는다. 그 전엔 콘솔에서 허브는 두 행을 같이 지울 것 |
| R7 | **허브 ack 의 구멍**: commands 에 없는 msg_id 는 카메라 ack 로 취급 → 기기 명령 ack 가 DB 지연으로 매칭 실패하면 경고 없이 넘어감 | 낮음 | `sweep_unacked` 가 timeout 처리. 빈도가 보이면 "허브 ack 미매칭" info 로그 추가 |
| R8 | **LCD**: 이식 완료. SPI3 20MHz 와 카메라 SDIO/CSI 가 같은 보드 — EMI 로 화면 깨짐 가능성. LCD 끈 빌드엔 `lcd_*` 가 `unknown_action` | 낮음 | `capabilities.lcd` 로 콘솔 패널 노출 판단(K-2). 깨지면 `APP_IOT_LCD_CLK_HZ` 10MHz 로 |
| R9 | **BLE 이름 `FB2_P4_CAM_`** 유지 → 앱은 "카메라 등록" 으로 보이지만 실제론 기기까지 생김. 사용자가 기기를 따로 또 등록하려 할 수 있음 | 중(UX) | K-3 에서 이름·안내 변경. 당장은 베타 설명서에 한 줄 |
| R10 | ~~GPIO 51 공유 여부~~ → **해결**: 회로도(DFR1237 V1.0)에서 51/A4 는 헤더 전용. 온보드 전용 핀은 3(LED) · 6(C6 WAKEUP) · 9/12(마이크) · 35(BOOT) 뿐 | — | 20~23/51/28/29/48~52 모두 헤더 전용 확인 |
| R11 | **같은 토픽에 두 스트림** — 3초 QoS0 + 15초 QoS1. PUBACK 건강 신호는 heartbeat 만 쓰므로 영향 없지만, 서버 `_sys_state` 가 두 경로에서 각각 uptime 을 보고 "재부팅 감지" 경고를 두 번 낼 수 있음 | 낮음 | 로그 중복만. 거슬리면 허브는 카메라 경로의 sys 경고를 생략 |
| R12 | **온도 보정 기본 -1.5℃** 는 S3 기기 보드 발열 기준. P4+카메라는 발열이 더 커서 DHT22 배치에 따라 더 높게 읽힐 수 있음 | 중 | DHT22 를 보드/카메라 모듈에서 떨어진 위치에, 실측 후 `set_temp_offset` 으로 보정 |

## 설계 메모

### 브리지 조회 비용
`_resolve_both` 는 접두사로 테이블을 고른다: `terra-` devices 만, `p4cam-/picam-` cameras 만, 그 외(`p4hub-`, 미지) 둘 다. 캐시 TTL(성공 5분/미존재 15초)은 그대로. 접두사는 서버가 발급하므로 신뢰해도 된다 — 바꾸면 `routers/cameras.py` prefix 와 `handlers._*_PREFIXES` 를 함께.

### 허브 ack 의 미세한 구멍
허브에서 `commands` 에 없는 msg_id 는 카메라 ack 로 본다. 기기 명령 ack 가 DB 지연으로 매칭에 실패해도 경고 없이 카메라 last_seen 만 갱신된다 → 그 명령은 `sweep_unacked` 가 timeout 처리. 순수 기기는 기존 경고 유지(회귀 테스트 있음).

### 토큰 해시 두 군데
같은 평문 토큰의 bcrypt 해시를 `cameras.token_hash` 와 `devices.token_hash` 양쪽에 저장한다(서로 다른 salt 라 문자열은 다름). 카메라 REST(Bearer) 인증은 `cameras` 만 보므로 기기 쪽 해시는 현재 참조처가 없다 — 형식 통일을 위해 넣어둔 것. `token_rotate` 구현 시 둘 다 갱신해야 한다.

### 펌웨어 LEDC 채널
조명 TIMER_1/CH_1, 펌프 소프트스타트 TIMER_2/CH_2. 카메라 트리는 LEDC 를 안 쓰므로 충돌 없음. (supermini 는 TIMER_0 이 LCD 백라이트였음 — 허브엔 없음.)

### 왜 BLE 이름을 아직 안 바꾸나
앱이 `FB2_P4_CAM_` 접두사로 카메라를 찾는다(`APP_REQUEST_DEVICE_PAIRING_2026-09-17.md` §5). 지금 바꾸면 앱 배포 전까지 허브를 등록할 길이 없다. 서버가 모델로 판단하므로 이름은 UX 문제일 뿐.

## 학습 노트
- C 주석 안에 `relay_*/mist` 처럼 `*/` 가 들어가면 주석이 거기서 끝난다. 첫 빌드가 이걸로 깨졌다 — 와일드카드 나열은 `·` 로.
- `-Werror` 빌드라 `iot/` 의 경고도 전부 에러. 현재 경고 0.
- 카메라 트리의 `app_mqtt_handlers_t` 는 "콜백 NULL = 기능 없음 = `rejected_unknown_action`" 규약이라 IoT 폴백도 같은 규약으로 끼워 넣으면 순수 카메라 빌드가 전혀 안 바뀐다.
- 서버 테스트의 Supabase mock 은 테이블명별 `side_effect` 팩토리 패턴(`_hub_bridge_tables`)이 제일 덜 깨진다. `MagicMock` 체인 기본값은 truthy 라 "행 없음" 분기를 못 탄다.
