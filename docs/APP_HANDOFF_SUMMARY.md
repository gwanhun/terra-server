# 앱 개발자 전달 — 신규/변경 기능 통합 요약 (2026-08-14)

> 그동안 추가된 앱 연동 계약을 한 문서로 정리. 기본 인증/Realtime 흐름은 **[APP_INTEGRATION.md](APP_INTEGRATION.md)**,
> 물분무·예약 상세는 **[APP_TIMER_MIST.md](APP_TIMER_MIST.md)** 참조.
>
> - **베이스 URL**: `https://api.terra-server.uk` — 모든 REST 호출에 `Authorization: Bearer <jwt>`
> - **명령 두 경로**: ① `commands` 테이블 직접 INSERT (Supabase, 기존 패턴) ② REST 엔드포인트(서버 검증)
> - **상태 추적**: 모든 명령은 `commands` 로 흐르므로 기존 `commands-rt` Realtime 구독으로 `pending→sent→acked` 추적

---

## 0. 한눈에 — 기능별 호출 방식

| 기능 | 방식 | 계약 |
|---|---|---|
| 물분무 | REST 또는 INSERT | `mist` + `{duration_ms:1000\|2000\|3000}` |
| on/off 제어 | INSERT | `relay_on/off`, `fan_on/off`, `heater_on/off`, `led_on/off` |
| 팬 타이머 | INSERT | `fan_on` + `{duration_ms}` (자동 OFF, 최대 2h) |
| LED 밝기 | INSERT | `led_on` + `{brightness:0~100}` (MOSFET 보드) |
| 예약 타이머 | REST | `POST/GET/PATCH/DELETE /devices/{id}/schedules` |
| 스마트 가드 | REST | 예약에 `guard` 필드 |
| LCD 텍스트 | REST | `POST /devices/{id}/lcd` |
| 감사 로그 출처 | 조회 | `commands.source` / `source_id` / `reason` |

---

## 1. 액추에이터 제어

### 1.1 on/off 절대 명령 (신규)
기존 `*_toggle` 외에 **절대 상태 명령**이 추가됐다. 멱등(이미 켜져 있어도 재전송 무해).
```dart
await sb.from('commands').insert({
  'device_id': deviceUuid, 'issued_by': sb.auth.currentUser!.id,
  'action': 'heater_on',   // relay_on/off, fan_on/off, heater_on/off, led_on/off
});
```
- **구간 예약(시작~종료)** 은 on/off 2건으로 구성한다 (toggle 은 상태 어긋남 위험).

### 1.2 물분무 (mist)
펌프를 N초만 켜고 **펌웨어가 자동 OFF**. OFF 명령 보내면 안 됨.
```dart
// 방법 A: commands 직접 INSERT (권장, 기존 패턴)
await sb.from('commands').insert({
  'device_id': deviceUuid, 'issued_by': sb.auth.currentUser!.id,
  'action': 'mist', 'payload': {'duration_ms': 2000},   // 1000|2000|3000
});
// 방법 B: REST (서버가 duration 검증)
// POST /devices/{deviceUuid}/mist  { "duration_ms": 2000 }
```

### 1.3 팬 타이머 (신규)
`fan_on` 에 `duration_ms` 를 주면 그 시간 뒤 **자동 OFF**. `fan_off` 로 타이머 취소.
```dart
await sb.from('commands').insert({
  'device_id': deviceUuid, 'issued_by': sb.auth.currentUser!.id,
  'action': 'fan_on', 'payload': {'duration_ms': 1800000},   // 30분. 최대 2h(7200000)
});
```
- 남은 시간 표시: 앱이 "발행시각 + duration" 으로 계산.
- ⚠️ **히터 타이머는 아직 미지원** (현재 보드에 히터 미탑재).

### 1.4 LED 밝기 (신규, 보드 의존)
`led_on` 에 `brightness`(0~100). `led_off` 또는 brightness 0 이면 꺼짐.
```dart
await sb.from('commands').insert({
  'device_id': deviceUuid, 'issued_by': sb.auth.currentUser!.id,
  'action': 'led_on', 'payload': {'brightness': 60},
});
```
- ⚠️ **MOSFET 보드만 실제 밝기 조절.** 릴레이 보드는 `brightness` 무시하고 켜기(on)만. 앱은 밝기 슬라이더를 MOSFET 보드에서만 노출하는 게 이상적(보드 타입 구분 필요 시 백엔드에 문의).

### 1.5 result 값 (acked 시) — 제어 디바이스(terra-iot-nano) 기준
| result | 의미 |
|---|---|
| `ok` | 성공 |
| `busy` | 이미 분무/타이머 진행 중 → 무시 |
| `bad_request` | 필수 payload 누락 (duration_ms 등) |
| `error` | 액추에이터 구동 실패 |
| `locked` | 히터 안전 latch 활성 → `heater_clear_lock` 먼저 |
| `unknown_action` | 펌웨어가 모르는 action (구버전) |
| `expired` / `duplicate` | TTL 만료 / 중복 msg_id |
> ⚠️ 카메라 펌웨어의 `rejected_*` 계열과 다름. **위 값 기준**으로 처리.

---

## 2. 예약 타이머 (schedules) — REST 전용

서버가 예약 시각에 `commands` 를 자동 발행. 실제 실행/상태는 §1 과 동일하게 `commands` 로 흐른다.

### 2.1 생성
```
POST /devices/{device_uuid}/schedules
```
```json
{
  "action": "mist",
  "payload": { "duration_ms": 2000 },
  "kind": "daily",                 // "daily" | "weekly"
  "time_of_day": "08:00",          // KST(한국시간) 기준
  "days_of_week": [1, 3, 5],       // weekly 필수. 1=월 … 7=일
  "guard": {                       // 선택 — 스마트 조건
    "type": "skip_when_humidity_above", "value": 70, "enabled": true
  }
}
```
| 필드 | 값 |
|---|---|
| `action` | `mist` \| `fan_on/off` \| `relay_on/off/toggle` \| `heater_on/off/toggle` \| `led_on/off/toggle` \| `fan_toggle` |
| `time_of_day` | `"HH:MM"` **KST** |
| `days_of_week` | `[1..7]` (weekly 필수) |
| `guard` | 아래 §2.3 |

응답의 `next_run_at` 은 **UTC** → 표시할 땐 KST 변환(`DateTime.parse(...).toLocal()`).

### 2.2 목록/수정/삭제
```
GET    /devices/{device_uuid}/schedules
PATCH  /schedules/{schedule_id}      { "enabled": false }  등 부분 수정
DELETE /schedules/{schedule_id}
```

### 2.3 스마트 가드 (guard)
예약 발행 직전 조건을 평가해 **스킵**한다. 서버가 처리하는 **skip 형**만 현재 지원:
| type | 동작 |
|---|---|
| `skip_when_humidity_above` | 습도 > value 면 이번 회차 스킵 |
| `skip_when_humidity_below` | 습도 < value 면 스킵 |
| `skip_when_temp_above` | 온도 > value 면 스킵 |
| `skip_when_temp_below` | 온도 < value 면 스킵 |
- `value` 는 숫자, `enabled` bool.
- 스킵되면 `commands` 에 `status='skipped', source='guard', reason='...'` 기록이 남는다(감사 로그).
- ⚠️ **stop 형**(가동 중 정지, 예: 히터 목표온도 도달 시 OFF)은 펌웨어 담당 — 아직 미구현(후속).

### 2.4 예약 실행 감지
예약 시각이 되면 서버가 `commands` INSERT → **기존 `commands-rt` 구독에 그대로 잡힘**. 별도 처리 불필요.

---

## 3. LCD 커스텀 텍스트 — REST 전용

디바이스 LCD 상단에 텍스트 표시(한글 가능). 서버가 비트맵으로 렌더해 전송, 디바이스에 저장(재부팅 유지).
```
POST /devices/{device_uuid}/lcd          { "text": "밥 6시" }
POST /devices/{device_uuid}/lcd/clear    (기본값으로 복귀)
```
- 빈 문자열(`text:""`) → clear 처리.
- **글자수**: 하드 상한 64자. 권장 **한글 ~8자 / 영문 ~12자** (넘으면 자동 축소). 폭 기준 한글 1자 ≈ 영문 2자.
- 응답: `{ "id", "action": "lcd_bitmap"|"lcd_clear", "status": "pending" }`. 상태는 `commands-rt` 로 추적.
- 앱 UI 권장: 글자수 카운터 + 권장 한도 힌트.

---

## 4. 감사 로그 — commands 출처 (신규 컬럼)

`commands` 에 출처 컬럼이 추가됐다. 앱 감사 로그 화면에서 "누가·왜 냈나" 필터/표시에 사용.
| 컬럼 | 값 |
|---|---|
| `source` | `manual`(수동) \| `schedule`(예약) \| `timer` \| `guard`(가드) |
| `source_id` | 연결된 `schedules.id` 등 (nullable) |
| `reason` | 가드 사유 등 (nullable, 예: "습도 72% > 70% → 스킵") |
```sql
-- 예약이 낸 명령만
SELECT * FROM commands WHERE device_id='<uuid>' AND source='schedule' ORDER BY issued_at DESC;
-- 가드로 스킵된 이력
SELECT action, reason, issued_at FROM commands WHERE source='guard';
```
- 앱이 직접 INSERT 하는 수동 명령은 `source` 미지정 시 자동으로 `'manual'`.

---

## 5. 배포/적용 상태 (참고)

| 항목 | 상태 |
|---|---|
| 백엔드(main) | 배포됨 (mist·예약·guard·source·LCD·on/off) |
| 마이그레이션 | `schedules`, `commands.source/source_id/reason`, `schedules.guard` 적용됨 |
| 펌웨어(terra-iot-nano) | 물분무·팬타이머·LED밝기·LCD 플래시 필요(디바이스 실동작) |
| 한글 LCD | 서버에 한글 폰트 설치 시 렌더 (영문은 무관) |

---

## 6. 앱 구현 체크리스트

- [ ] 물분무 1/2/3초 버튼 (`mist` + duration_ms)
- [ ] on/off 제어 버튼 (`*_on`/`*_off`) + 구간 예약은 2건 구성
- [ ] 팬 타이머 (`fan_on` + duration_ms) + 남은시간 = 발행시각+duration
- [ ] LED 밝기 슬라이더 (`led_on` + brightness) — MOSFET 보드
- [ ] 예약 CRUD (daily/weekly, KST 시각, 가드) + `next_run_at` KST 변환
- [ ] LCD 텍스트 입력 (글자수 힌트 한글~8/영문~12)
- [ ] 감사 로그 (source 필터 + reason 표시)
- [ ] result 값(`busy`/`error`/`locked`/`unknown_action`) UI 처리
