# 백로그 — 나중에 반영할 것들

> 당장 안 하지만 잊으면 안 되는 작업 모음. 스테이지 스펙(`specs/`)으로 승격되기 전 단계.
> 항목마다 **왜 / 무엇 / 어디** 를 적고, 착수하면 스펙으로 옮기고 여기서는 상태만 바꾼다.
> 상태: 🕐 대기 / 🚧 진행 중 / ✅ 반영됨 (반영 커밋 링크) / 🗑️ 폐기

| 상태 | 항목 | 등록일 |
|------|------|--------|
| 🕐 | [1. Wi-Fi 신호 세기(RSSI) 수집 + 기기 TX 출력 재조정](#1-wi-fi-신호-세기rssi-수집--기기-tx-출력-재조정) | 2026-09-23 |
| 🚧 | [2. 펌웨어 원격 업데이트(OTA) — 카메라·기기](#2-펌웨어-원격-업데이트ota--카메라기기) | 2026-10-06 |

---

## 1. Wi-Fi 신호 세기(RSSI) 수집 + 기기 TX 출력 재조정

**등록**: 2026-09-23 · **상태**: 🕐 대기

### 배경 (왜)

베타에서 Wi-Fi 품질이 나쁜 집일수록 카메라·기기 장애가 잦은데, **RSSI 를 어디서도 수집하지 않아 "신호가 나빠서" 인지 확인할 방법이 없다.** 2026-09-23 조사 결과:

| 위치 | 상태 |
|------|------|
| 카메라 heartbeat (`firebeetle2-p4-yr030/main/app_mqtt.c` telemetry_task) | ts·uptime·heap·reset·clips·img·hw_id 만. RSSI 없음 |
| 기기 telemetry (`terra-iot-supermini/main/src/mqtt_app.c`) | 온습도·릴레이·팬·LED·hw_id 만. RSSI 없음 |
| 서버 `backend/mqtt/handlers.py` | rssi 필드 파싱 없음 |
| DB `cameras` / `devices` / `telemetry` | rssi 컬럼 없음 |

기기 펌웨어는 끊김 순간에만 시리얼 로그로 `disconnected: reason=.. rssi=..` 를 찍는다 (`wifi.c` 이벤트 핸들러). 시리얼 없는 베타 보드에선 볼 수 없다.

2026-09-23 02:14 UTC DB 스냅샷: 활성 카메라 13대 중 온라인 0대, 활성 기기 14대 중 12대 온라인. 같은 집 쌍(8636·9175·1840)에서 기기는 살고 카메라만 죽는 패턴 반복. 다만 카메라 전원을 사용자가 뺀 경우도 섞여 있어 RSSI 없이는 단정 불가.

### 현재 펌웨어 Wi-Fi 설정 (조사 기준 2026-09-23)

| 항목 | 카메라 (P4 + C6, esp_wifi_remote) | 기기 (supermini / nano S3) |
|------|------|------|
| 전력 모드 | `WIFI_PS_NONE` | `WIFI_PS_MIN_MODEM` |
| 송신 출력 | 기본값 (C6 최대 ≈ 20dBm) | **8.5dBm (`esp_wifi_set_max_tx_power(34)`)** |
| 끊김 후 재연결 | 무한, 1~2초 자연 간격 | 무한, 1→30초 지수 백오프 |
| 최후 복구 | IP 90초 미복구 → 자동 재부팅 (`net_wd`) | 없음 |
| MQTT keepalive | 30초 + PUBACK 45초 무응답 시 소켓 재생성 | 60초, QoS0 |
| heartbeat 주기 | 15초 | 3초 |
| 서버 오프라인 판정 | 180초 (`offline_monitor.py`) | 180초 |

- **기기 8.5dBm 제한**은 2026-09-17 07 보드에서 펌프 기동 + Wi-Fi 피크가 겹쳐 브라운아웃이 나서 일부러 낮춘 것. 기본 대비 약 11dB 낮아 같은 공유기에서 카메라보다 먼저 끊긴다. 펌웨어 주석에 "전원 보강 후 40(10dBm) → 52(13dBm) 단계적으로 올릴 것" 명시.
- **카메라 net_wd 재부팅**은 Wi-Fi 가 90초만 IP 를 잃어도 발동 → 부팅 + 재연결 + 15초 heartbeat 로 온/오프라인 깜빡임. 라이브 중이면 WebRTC 세션도 끊김.
- 베타에 배포된 카메라 펌웨어는 `clip_stats` 가 비어 있어 레포 최신보다 옛 버전. uptime/reset 진단(`clip_stats.sys`)도 안 올라온다 → 재부팅 원인조차 못 본다.

### 할 일 (무엇)

**① 펌웨어 — heartbeat 에 `wifi` 블록 추가 (카메라·기기 둘 다)**

```json
"wifi": {"rssi": -67, "ch": 6, "disc": 3, "reason": 201}
```

- `rssi`/`ch`: `esp_wifi_sta_get_ap_info()` 한 번.
- `disc`: 부팅 후 `WIFI_EVENT_STA_DISCONNECTED` 누적 횟수. `reason`: 마지막 disconnect reason 코드 (201 = NO_AP_FOUND, 2 = AUTH_EXPIRE, 8 = ASSOC_LEAVE, 200 = BEACON_TIMEOUT 등). 끊기는 **이유**가 서버에서 보이게.
- 카메라는 `payload[800]` 버퍼 여유 확인 (현재 clips 320 + img 120 + 나머지). 기기는 `payload[448]` 이라 60B 정도 늘려야 함.
- 카메라 펌웨어 재배포 시 clips/sys 도 함께 올라가므로 재부팅 사유 진단도 같이 해결됨.

**② 서버 — 저장**

- 카메라: `cameras.net_state JSONB` 한 컬럼 (마이그레이션 1건). `handle_telemetry` 카메라 분기에서 `payload["wifi"]` 를 그대로 저장. 값이 같으면 UPDATE 생략(Realtime 잡음 억제, capabilities 와 같은 패턴).
- 기기: `telemetry.rssi SMALLINT` 컬럼 추가 (마이그레이션 1건) + `telemetry_30m` 집계에 avg/min 태우기. 기기 `net_state` 는 `devices` 에 JSONB 로 disc/reason 만.
- `docs/MQTT.md` §1 스펙 갱신, `tests/test_mqtt_handlers.py` 케이스 추가.
- 경고 로그: `rssi <= -80` 이면 bridge 에서 `warning` 1회 (히스테리시스 5dB).

**③ 운영 — 기기 TX 출력 단계적 상향**

- all-MOSFET 정책 이후 전원 보강된 보드부터 `esp_wifi_set_max_tx_power(40)` (10dBm) 로 한 단계. 브라운아웃 재발 없으면 52(13dBm).
- RSSI 가 쌓이면 -75dBm 이하 기기부터 골라 적용. 판단 기준: -67 이상 안정 / -70~-75 한계 / -80 이하 heartbeat 도 유실.

**④ (검토) 카메라 net_wd 90초 → 재연결 여유**

- 신호 약한 집에서 재부팅 루프가 관찰되면 `NET_WATCHDOG_SEC` 상향 또는 "IP 상실 후 재연결 시도 중이면 타이머 리셋" 로 완화 검토. RSSI 데이터 나온 뒤 결정.

### 확인 필요 (착수 전)

- [ ] 서버 저널로 카메라 끊김 사유 실측 (내 SSH 키로 접속 불가, 사용자가 직접):
  ```bash
  sudo journalctl -u mosquitto --since "24 hours ago" | grep -E "p4cam|terra-" | grep -iE "disconnect|closed|timeout"
  sudo journalctl -u terra-bridge --since "24 hours ago" | grep -E "재부팅|정체|오프라인"
  ```
- [ ] 베타 카메라 펌웨어 실제 버전 확인 (`clip_stats` 비어 있음 → 옛 빌드 추정)

### 관련

- 펌웨어: `~/project/esp32/firebeetle2-p4-yr030/main/{app_mqtt.c,main.c}`, `~/project/esp32/terra-iot-supermini/main/src/{mqtt_app.c,wifi.c}`
- 서버: `backend/mqtt/handlers.py` (`handle_telemetry`), `backend/offline_monitor.py`
- 배경 결정: 기기 TX 8.5dBm 사유는 `wifi.c` 주석 (2026-09-17 브라운아웃), MOSFET 통일 정책은 memory `project_iot_board_all_mosfet`

---

## 2. 펌웨어 원격 업데이트(OTA) — 카메라·기기

**등록**: 2026-10-06 · **상태**: 🚧 스펙 승인(2026-10-07) → [specs/stage-j-ota.md](../specs/stage-j-ota.md)

### 배경 (왜)

9/28 이후 펌웨어 수정(WiFi 재연결·업로드 정체 자가 재부팅·인코더 스톨 워치독·에러 로그)이 베타 카메라 18대에 거의 반영되지 않았다. OTA 가 없어 매 변경이 물리 리플래시라서다(`firebeetle2-p4-yr030/docs/IMPROVEMENTS.md` §14~§17, `docs/C6_BLE_RECOVERY.md` "원격 복구 경로가 없다"). 유일한 원격 수단은 `reboot` 명령.

### 핵심 사실 (2026-10-06 조사)

- 카메라 P4: 플래시 **설정 2MB / 실제 16MB**, factory 단일 1900K 에 앱 1.74MB. 공간이 아니라 설정이 OTA 를 막고 있었다.
- 기기 S3 nano: 4MB 설정 = 실제 4MB(ESP32-S3FH4R2 내장 플래시, 증설 불가), 앱 1.3MB → 1.9MB 슬롯 2개 가능(여유 600K). **supermini 는 범위 제외**(owner 결정 2026-10-06).
- 두 쪽 모두 파티션 테이블 변경이 필요 → **마지막 물리 리플래시 1회** 불가피. §15 묶음 빌드와 같은 회차로.

### 할 일

스펙 참조. 요약: 서버(릴리스 테이블·`POST /{cameras,devices}/{id}/ota`·토큰 인증 바이너리 프록시·ack/heartbeat 로 job 추적·콘솔) + 펌웨어(16MB·ota_0/1·`esp_https_ota`·부팅 시 적용·부트로더 롤백·heartbeat `fw`).

### 관련

- 펌웨어: `~/project/esp32/firebeetle2-p4-yr030/{partitions.csv,sdkconfig,main/app_mqtt.c}`, `~/project/esp32/terra-iot-nano/main/src/command_dispatch.c`
- 서버: `backend/mqtt/camera_commands.py`, `backend/routers/cameras.py`(reboot 패턴), `backend/mqtt/handlers.py`
