# 앱 전달 — 백엔드 §2~§6 반영 완료 (2026-08-18)

> **받는 쪽**: 앱(Flutter)
> **성격**: 2026-08-14 / 08-18 핸드오프 요구를 백엔드에 구현·배포 완료. 앱이 바로 통합할 수 있게 **API 계약 + 앱측 변경점** 정리.
> **배포 상태**: `main` 반영 + 운영(`api.terra-server.uk`) 재시작 완료. 마이그레이션 4개 Supabase 적용 완료.

---

## 0. 한눈에

| # | 항목 | 엔드포인트/필드 | 앱측 할 일 |
|---|---|---|---|
| §2 | 보드 능력 | `devices.capabilities` (JSONB) | `led_dimmable` 로 밝기 슬라이더 조건부 노출 |
| §5 | 목표 환경 | `GET/PATCH /devices/{id}/settings` | setpoint 하드코딩 제거 → 이 API 사용 |
| §3 | 구간 예약 | `schedules.pair_id` | `addSpan` = 같은 `pair_id` 2건, 삭제는 1건만 |
| §4 | LED 상태 | `telemetry.led` / `led_brightness` | `_ledOn` 로컬 추측 제거 → telemetry 사용 |
| §6 | 예약 toggle 금지 | 서버 400 | 이미 반영됨(절대명령만) |
| §4-3 | off+guard 금지 | 서버 400 | off 예약엔 guard 안 붙이면 됨 |
| §1 | 카메라 IDR | (보류) | 변경 없음 — 아래 참고 |

---

## §2 — `devices.capabilities` (보드 능력)

`GET /devices`, `GET /devices/{id}` 응답의 `DeviceOut` 에 **`capabilities`** 필드 추가(JSONB, nullable).

```jsonc
{
  "id": "…", "device_id": "terra-ab260087", "name": "…",
  "capabilities": { "board": "mosfet", "led_dimmable": true }
  // 릴레이 보드: { "board": "relay", "led_dimmable": false }
}
```

**앱 적용**: `capabilities?.led_dimmable == true` → 밝기 슬라이더, 아니면 ON/OFF 버튼.

**⚠ 현재 한계(중요)**: **펌웨어가 아직 capabilities 를 보고하지 않는다.** 그래서:
- 마이그레이션이 **기존 기기를 전부 `{board:"relay", led_dimmable:false}` 로 백필**했다.
- 즉 실제 MOSFET 보드라도 지금은 `relay` 로 보여 밝기 슬라이더가 안 뜬다.
- 임시로는 운영자가 DB 에서 해당 기기 `capabilities` 를 `mosfet` 으로 갱신해야 정확.
- **근본 해결(예정)**: 펌웨어 페어링 시 `capabilities` 보고 추가 → 이후 신규/재페어링 기기는 자동. (별도 트랙)

앱은 지금 **`led_dimmable` 기준 분기 로직만 넣어두면**, 펌웨어 보고가 붙는 순간 자동으로 맞는다.

---

## §5 — 목표 환경 setpoint (`device_settings`)

신규 REST. 소유권 검증 + 서버측 범위 검증.

### `GET /devices/{id}/settings` → 200
```jsonc
{
  "device_id": "…",
  "target_temp_c": 28.0,          // 단일 목표 온도(℃)
  "target_humidity_pct": 60.0,    // 단일 목표 습도(%)
  "target_temp_min": 24.0, "target_temp_max": 32.0,
  "target_humid_min": 40.0, "target_humid_max": 70.0,
  "updated_at": "…"
}
```
- **미설정 기기도 404 아님** → 값이 전부 `null` 인 200 반환. (앱은 null 처리)

### `PATCH /devices/{id}/settings` → 200 (부분 수정, 없으면 생성=upsert)
```jsonc
// 보낸 필드만 갱신
{ "target_temp_c": 30, "target_humidity_pct": 55 }
```
**검증(위반 시 400)**: 온도 −20~60℃, 습도 0~100%, `*_min ≤ *_max`. 남의 기기 404.

**앱 적용**: `module_status_card` 의 목표값 하드코딩 제거 → `GET` 으로 로드, 사육장 설정에서 `PATCH`.

---

## §3 — 구간 예약 `pair_id`

구간(시작~종료)은 서버에 여전히 **on/off 2행**이지만, 두 행에 같은 `pair_id`(앱 생성 UUID)를 넣으면 서버가 짝을 묶는다.

### 생성 — `POST /devices/{id}/schedules` 에 `pair_id` 추가
```jsonc
// 시작(on)
{ "action":"heater_on",  "kind":"daily", "time_of_day":"20:00", "pair_id":"<uuid>" }
// 종료(off)
{ "action":"heater_off", "kind":"daily", "time_of_day":"06:00", "pair_id":"<uuid>" }
```
- `GET /devices/{id}/schedules` 응답에 `pair_id` 포함 → 앱이 목록을 **구간 한 줄**로 묶어 표시.

### 삭제 — `DELETE /schedules/{schedule_id}`
- 그 예약의 `pair_id` 가 있으면 **짝(on/off) 함께 삭제** (고아 예약 방지).
- `pair_id` 가 없으면(단건) 그 행만 삭제.

**앱 적용**: `addSpan` = UUID 1개 생성 → on/off 2건 POST(같은 `pair_id`). 삭제는 둘 중 아무거나 1건 DELETE 하면 서버가 짝도 지움. (자정 넘김 요일 밀기 등은 기존대로 앱이 계산)

---

## §4 — `telemetry.led` 상태

`telemetry` 행에 컬럼 추가:
- `led`: `"ON"` | `"OFF"` | `null`
- `led_brightness`: `0~100` | `null` (MOSFET 보드만; 릴레이는 null)

**앱 적용**: LED 를 `_ledOn` 로컬 플래그(낙관적)로 표시하던 것 제거 → telemetry `led`/`led_brightness` 로 실제 상태 표시. (앱 재시작·다기기·예약 실행 후에도 실제와 일치)

> 참고: 실제 값이 뜨려면 해당 기기가 **§4 반영 펌웨어**로 플래시돼 있어야 함(구 펌웨어는 `led` 미전송 → null).

---

## §6 — 예약 `*_toggle` 금지

`POST`/`PATCH /schedules` 에서 `action` 이 `*_toggle`(relay/fan/heater/led)이면 **400**.
- 허용 action: `mist`, `relay_on/off`, `fan_on/off`, `heater_on/off`, `led_on/off`.
- 앱은 이미 절대명령만 쓰므로 변경 없음. (다른 클라이언트 대비 서버가 강제)
- **즉시 제어(commands 직접 발행)에는 toggle 계속 사용 가능** — 예약 경로만 막음.

---

## §4-3 — off 예약 + guard 금지

`action` 이 `*_off` 인 예약에 `guard` 를 붙이면 **400** (생성·수정 양쪽).
- 이유: off 예약이 스킵되면 기기가 켜진 채 남아 위험(히터 과열).
- 앱 적용: guard(스마트 조건)는 **on/mist 예약에만** 붙인다. off 예약엔 guard 없이.
- `PATCH {"guard": null}` 은 가드 해제로 정상 동작(400 아님).

---

## §1 — 카메라 첫 프레임 18초 (보류)

**앱 변경 없음.** 진단(연결 시 IDR 미송출)은 정확하나, 현재 컴포넌트(esp_video 2.2.0)가 **force-key-frame 컨트롤을 미지원**해 ①IDR-on-connect·②PLI 대응이 불가함을 확인. 선택지: A) esp_video 업그레이드, B) GOP(I_PERIOD) 축소(클립 크기 트레이드오프), C) 라이브 인코더 분리. **하드웨어 담당 결정 대기.** 상세: `docs/BACKEND_HANDOFF_REPLY_2026-08-18.md` §1.

---

## 배포/마이그레이션 상태

- **코드**: `main` 반영 + 운영 재시작 완료.
- **마이그레이션(Supabase 적용 완료)**: `2026-08-18_devices_capabilities` / `device_settings_setpoint` / `schedules_pair_id` / `telemetry_led`.
- **웹 콘솔**: 테스트 UI 반영(목표환경 패널·LED 컬럼·보드별 LED 제어·예약 on/off).

## 남은 것 (백엔드/펌웨어 트랙)
- 펌웨어 페어링 시 `capabilities` 보고 (→ §2 자동화). **미구현**
- 펌웨어 `telemetry.led` publish 는 nano/relay 반영·플래시 완료.
- 카메라 §1 방향 결정.
