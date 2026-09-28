# 앱 전달 — IoT 기기 원격 재부팅 API + 상태 진단 필드 (`devices.sys_state`)

> **대상**: 앱 개발 담당
> **작성**: terra-server 백엔드 담당, 2026-09-28
> **성격**: 카메라에 넣었던 원격 재부팅과 상태 진단(rssi · 재부팅 사유 · 가동시간)을 **사육장 IoT 기기(terra-iot-nano / supermini)** 에도 동일하게 넣었습니다. 앱에 **새 API 1개**와 **`devices` 응답 필드 1개**가 생겼습니다. 필수 작업은 없고, 재부팅 버튼은 **권장**입니다.
> **관련 커밋**: 서버 `080dd8e`(sys_state 저장 · reboot API · 콘솔) + `6edcbe8`(마이그레이션 적용 기록) + PR #6(기기 heartbeat 폴백) + 예약 복원(§2.4). 펌웨어 nano `5900a87` / supermini `bfbccc4`. 카메라 쪽 문서: [APP_CAMERA_REBOOT_HEALTH_2026-09-28.md](APP_CAMERA_REBOOT_HEALTH_2026-09-28.md)
> **개정 이력**: 9/28 오후 — petcam 회신 반영(앱 결정 3건 확정 · 🔴 재부팅 후 예약 상태 복원 구현(§2.4) · §2.2 `result` 판단 · §2.3 정정 · PR #6 머지).

---

## 0. 요약

| # | 항목 | 앱에서 할 일 | 권장도 |
|:---:|---|---|:---:|
| ① | `POST /devices/{id}/reboot` — 원격 재부팅 | 기기 상세에 "기기 재시작" 버튼 (§2) | 권장 |
| ② | `devices.sys_state` — 가동시간 · 재부팅 사유 · 힙 · WiFi 신호 | 신/구 펌웨어 판별 기준으로 사용 (§3) | 권장 |
| ③ | `sys_state.rssi` — WiFi 신호(dBm) | **−75 이하 = 약함** 일 때만 설치 위치 안내 (§3, 카메라와 같은 경계) | 선택 |
| ④ | `sys_state.reset` — 마지막 재부팅 사유 | 지원 화면에 표시하거나 무시 (§3) | 선택 |
| ⑤ | 재부팅 후 예약 상태 복원 (§2.4, petcam 요청) | 앱 동작 변화 없음. `commands.source == "restore"` 행이 새로 보임 | — |

**앱 결정(9/28 회신)**: 버튼은 기기 상세 화면 · rssi 는 −75 이하가 약 1분(20회 연속) 이어질 때만 안내 · reset/heap 은 앱에서 미사용. 카메라와 동일.

**`sys_state` 는 신 펌웨어부터 옵니다. 리플래시 전에는 `null` 이 정상이라 앱은 없음을 기본으로 처리해 주세요.**

---

## 1. 배경 — 왜 넣었나

기기가 "온라인인데 명령에 반응이 없다" 또는 "WiFi 가 자주 끊긴다" 는 상태가 되면 지금까지는 사용자가 전원을 뽑는 방법뿐이었고, 서버도 시리얼 없이는 원인(전원 부족 브라운아웃인지, 크래시인지, WiFi 가 약한지)을 알 수 없었습니다.

카메라 보드에 9/28 넣은 것과 같은 계약으로

- 기기가 3초 telemetry 에 **가동시간 · 남은 힙 · 마지막 재부팅 사유 · WiFi RSSI** 를 실어 보내고, 서버가 최신값을 `devices.sys_state` 에 저장합니다(telemetry 행에는 넣지 않음).
- **원격 재부팅 명령**을 추가했습니다. 카메라와 달리 기존 `commands` 테이블을 거치므로 앱이 이미 쓰는 `commands` Realtime 으로 결과(acked / no_ack)를 그대로 봅니다.

---

## 2. `POST /devices/{device_uuid}/reboot`

### 2.1 계약

```
POST /devices/{device_uuid}/reboot
Authorization: Bearer <사용자 JWT>
(본문 없음)

→ 201 { "id": "<commands.id>", "action": "reboot", "status": "pending" }
→ 404 { "detail": "..." }        타인 기기 / 등록 해제된 기기 (다른 기기 API 와 동일 규칙)
→ 500                            commands INSERT 실패 (드묾)
```

- 응답 모양은 `POST /devices/{id}/mist` 와 같습니다(`CommandOut`).
- `commands` 행이 생기므로 **`commands` 직접 INSERT 로도 가능**합니다(`action: "reboot"`, `ttl_sec: 60` 권장). REST 를 쓰면 서버가 TTL 60초를 넣어 주고 소유권 검증을 대신합니다.
- 발행은 브리지(dispatcher)가 1초 폴링으로 담당합니다. 발행 실패 시 pending 으로 남아 다음 폴링에 재시도됩니다.

### 2.2 기기 쪽 동작

| 상황 | 결과 |
|---|---|
| 온라인 + 신 펌웨어 | ack(`result: "ok"`, `state: "REBOOT"`) 를 먼저 보내고 **1.5초 뒤 재부팅**. 액추에이터(펌프·팬·조명)는 부팅 초기 블록이 전부 OFF 로 잡으므로 분무 중이어도 안전 |
| 온라인 + 구 펌웨어 | `result: "unknown_action"` 으로 ack → **아무 일도 일어나지 않음**. 이때도 `commands.status` 는 **`acked`** 이므로 `status` 만 보면 "완료"로 보입니다. 성공 판정은 반드시 **`result == "ok"`** 로 하세요 |
| 오프라인 | TTL 60초 안에 기기가 없으면 유실. 서버가 30초 무응답이면 `status: "no_ack"` 로 굳힘 |

- **재부팅 완료 판정**: `commands` 가 `acked` 로 바뀐 뒤, `devices` Realtime UPDATE 에서 `sys_state.uptime_s` 가 작아지고 `sys_state.reset == "SW:mqtt_reboot"` 이면 재부팅이 끝나고 첫 telemetry 가 온 것입니다.
- 소요 시간은 실기 측정 전입니다. 카메라 실측(약 12초)과 같은 WiFi/MQTT 경로라 **10~20초** 로 예상하며, 측정되면 이 줄을 갱신합니다.
- 오프라인 판정 임계는 180초라 정상 재부팅이면 `is_online` 은 `false` 로 뒤집히지 않습니다.

### 2.3 UX 권장

- 노출 조건: `is_online == true` **이고** `sys_state != null`(§3). 구 펌웨어에서는 눌러도 반응이 없어 사용자가 혼란스럽습니다.
- 확인 다이얼로그: "기기가 약 20초 동안 꺼졌다 켜집니다. 켜져 있던 펌프·팬·조명은 모두 꺼집니다."
- 연타 방지: 발행 성공 후 60초 비활성(명령 TTL 과 동일).
- `unknown_action` ack 를 받으면 "기기 펌웨어 업데이트가 필요합니다" 안내.
- 재부팅 뒤 조명 등 상태는 **모두 OFF 에서 시작**합니다. 예약으로 켜져 있던 조명·팬은 서버가 **재부팅을 감지해 다시 켭니다**(§2.4). 복원 배포 전까지는 다음 예약 시각까지 꺼진 채이므로, 그때까지 다이얼로그에 "예약으로 켜져 있던 조명·팬은 다음 예약 시각까지 꺼져 있을 수 있어요" 문구를 두기로 한 것(petcam 회신)에 동의합니다. 배포되면 §5 를 갱신하고 알리겠습니다.

### 2.4 재부팅 후 예약 상태 복원 (petcam 회신 §2, 서버 구현)

**문제**: 예약 러너는 예약 시각에만 명령을 큐잉해서, 조명 08:00 ON / 20:00 OFF 인 기기가 14:00 에 재부팅하면 다음 날 08:00 까지 꺼진 채였습니다. 원격 재부팅뿐 아니라 BROWNOUT·정전·크래시 재부팅에서도 같습니다.

**동작** (`backend/schedule_restore.py`): 브리지가 기기 telemetry 의 `uptime_sec` 으로 재부팅을 감지하면, 그 기기의 활성 예약 중 **켜짐/꺼짐 상태가 있는 것**만 골라 액추에이터별로 "지금 이전 가장 최근 이벤트"를 계산하고, 그것이 ON 이면 ON 명령을 **1회** 큐잉합니다.

| 항목 | 결정 |
|---|---|
| 감지 | `uptime_sec` 감소. 브리지 재시작 직후(직전 값 없음)엔 uptime 60초 미만인 첫 telemetry 만 재부팅으로 봄. **부팅 1회당 1번**(boot 시각 기억) |
| 대상 | 조명 `led_on/off`(밝기 포함) · 팬 `fan_on/off` · 냉각팬 `fan2_on/off` |
| 제외 | `mist`/`spray_*`(1회성) · `relay_on/off`(펌프 — 연속 가동 복원은 침수 위험) · `*_toggle`(절대 상태 아님) · `payload.duration_ms` 가 있는 one-shot 예약(팬 타이머) · 히터(펌웨어 미구현) |
| 계산 | 액추에이터마다 daily/weekly 예약의 마지막 발생 시각(KST)을 구해 가장 최근 것이 ON 이면 큐잉. weekly 는 해당 요일의 마지막 발생 |
| 가드 | `skip_when_*` 를 **복원에도 적용**. 원래 시각에 스킵됐을 조건이면 지금도 켜지 않음(사용자의 스마트 조건 우선). `stop_when_*` 는 펌웨어 담당이라 무관 |
| source | `commands.source = "restore"`, `source_id = schedules.id`, `reason = "reboot_restore"`. 예약 푸시(`source="schedule"` 만)는 나가지 않음 |
| 시점 | 재부팅 후 첫 telemetry(부팅 약 10초 뒤) 수신 즉시 큐잉 → 브리지 1초 폴링으로 발행 |

**앱에서 할 일**: 없습니다. 명령 이력에 `source: "restore"` 행이 보이면 "재부팅 후 자동 복원"으로 표시해 주면 좋고, 그냥 두어도 됩니다. `commands.source` 허용값에 `restore` 가 추가됐습니다(DB CHECK, §5 마이그레이션).

---

## 3. `devices.sys_state` 구조

`GET /devices`, `GET /devices/{id}` 응답과 `devices` Realtime UPDATE 에 포함됩니다. 3초 telemetry 마다 갱신됩니다.

```json
"sys_state": {
  "uptime_s": 61831,
  "reset": "SW:mqtt_reboot",
  "heap": 190000,
  "rssi": -68
}
```

| 필드 | 단위 | 앱 활용 | 펌웨어 |
|---|---|---|---|
| (전체) `null` | — | **구 펌웨어** 판별 기준. 기기는 카메라와 달리 `firmware_ver` 를 페어링 때만 보내므로 버전 문자열로 판별하지 마세요 | 구 |
| `rssi` | dBm (음수, 0 에 가까울수록 좋음) | **−75 이하 = 약함**(웹 콘솔 빨강과 동일 경계). 참고 구간: −60 이상 좋음 / −61~−74 보통 / −75 이하 약함. 약함일 때만 "공유기와 가까운 곳에 설치" 안내. WiFi 미연결 순간엔 키 자체가 없을 수 있음 | 신 |
| `uptime_s` | 초 | 재부팅 판정(§2.2), "가동 N일" 표시 | 신 |
| `reset` | 문자열 | 마지막 재부팅 사유. 사용자 노출은 권장하지 않고 지원 화면 정도 | 신 |
| `heap` | 바이트 | 무시해도 됨(서버 감시용) | 신 |

**상대 시간 주의**: `uptime_s` 는 마지막 telemetry 시점(`last_seen_at`) 기준입니다. `is_online == false` 면 마지막 값이 그대로 남으므로 "마지막 보고 기준" 으로 표시하거나 숨기는 게 맞습니다.

`reset` 값 목록:

| 값 | 뜻 |
|---|---|
| `POWERON` | 전원 투입 |
| `SW:mqtt_reboot` | 앱/서버의 원격 재부팅 (§2) |
| `USB` | USB 로 리플래시 직후 |
| `BROWNOUT` | 전원 부족 — 펌프 기동 시 전압 강하 등. 어댑터·케이블 문제 신호 |
| `PANIC` / `WDT` / `INT_WDT` / `TASK_WDT` | 크래시 |
| `SW` | 펌웨어 자체 재부팅인데 사유 기록이 없는 경우 (드묾) |

---

## 4. 앱 계약 변경 없음 확인

- 기존 필드·응답 모양은 그대로입니다. `DeviceOut` 에 `sys_state` 하나가 늘었고 구 펌웨어는 `null` 입니다.
- `telemetry` 테이블 스키마 변화 없음. 진단 필드는 `telemetry` 행에 들어가지 않습니다.
- `devices` Realtime UPDATE 빈도: 원래 3초마다 `last_seen_at` 갱신으로 이미 오던 것이라 **늘지 않습니다**. 행 크기만 약 80B 커집니다.
- 명령 상태 흐름·`result` 값·푸시 파이프라인 변화 없음. `reboot` 도 다른 액션과 같이 `pending → sent → acked` 로 흐릅니다.

---

## 5. 반영 상태와 확인 방법

| 구분 | 상태 |
|---|---|
| DB 마이그레이션 (`devices.sys_state`) | **운영 적용 완료** (2026-09-28, petcam 9/28 조회로도 확인) |
| DB 마이그레이션 (`commands.source` 에 `restore`) | **미적용** — `2026-09-28_commands_source_restore.sql`. 복원 코드 배포 전에 적용. 없으면 복원 INSERT 만 실패하고 다른 동작엔 영향 없음 |
| 서버 코드 | `main` 반영: `080dd8e`(reboot·sys_state) → PR #6 머지(기기 heartbeat 폴백) → 예약 복원(§2.4). pytest 344 passed |
| 서버 운영 | **배포 예정** — 배포되면 이 줄 갱신. 배포 전엔 `POST /devices/{id}/reboot` 가 404(라우트 없음), `sys_state` 는 항상 `null`, 복원 없음 |
| 펌웨어 | 빌드 완료(nano·supermini). **실기 검증 전** — 플래시 후 콘솔에서 재부팅 → `reset=SW:mqtt_reboot` 수신을 확인하고 이 줄 갱신 |

앱 쪽 테스트는 신 펌웨어를 플래시한 기기 1대로 §2.2 재부팅 판정과 §3 필드 표시를 확인하고, 구 펌웨어 기기로 "없음 처리" 를 확인해 주세요. 어느 기기가 신 펌웨어인지는 별도 공지합니다.

---

## 6. 결정 요청 — 9/28 회신으로 확정

1. "기기 재시작" 버튼 → **기기 상세 화면**, 노출 조건 `is_online && sys_state != null`.
2. rssi → 평소 비노출, **−75 이하가 약 20회(≈1분) 연속**일 때만 설치 위치 안내.
3. `reset` · `heap` → **앱 미사용**.

남은 판단 1건(서버 쪽): 복원 시 가드 적용 여부는 **적용**으로 정했습니다(§2.4). 이견 있으면 알려주세요.

관련 정본: [API.md §3.7-b](API.md) (`POST /devices/{id}/reboot`, `sys_state`), [MQTT.md §1·§2](MQTT.md) (telemetry 필드, `reboot` action), [APP_INTEGRATION.md](APP_INTEGRATION.md) §3.7-b 링크.
