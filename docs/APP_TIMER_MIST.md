# 앱 통합 — 물분무(mist) + 예약 타이머 (Stage H)

> 신규 기능 2종을 앱에 붙이는 가이드. 기본 인증/Realtime 흐름은 **[docs/APP_INTEGRATION.md](APP_INTEGRATION.md)** 를 먼저 숙지.
> 참조 구현: [web/index.html](../web/index.html) — 💦 물분무 버튼 + ⏰ 예약 타이머 패널이 실동작 코드로 들어있음.

- **베이스 URL**: `https://api.terra-server.uk` (REST). 모든 REST 호출에 `Authorization: Bearer <jwt>`.
- **DB 마이그레이션**: `schedules` 테이블 적용 완료. 서버는 main 배포 반영 후 사용 가능.
- **⚠️ firmware 의존**: 물분무는 디바이스 펌웨어가 `mist` action 을 지원해야 실제 펌프가 돈다.
  미지원 펌웨어면 명령은 정상 발행되지만 `result='unknown_action'` 으로 돌아온다.

---

## 1. 물분무 (mist)

펌프를 **정해진 시간(1/2/3초)만 켜고 자동으로 끈다.** 펌웨어가 내부 타이머로 자동 OFF 하므로,
앱은 "얼마나(duration)"만 보내면 된다. **OFF 명령을 따로 보낼 필요 없음** (그렇게 하면 안 됨).

### 1.1 발행 — 방법 A: `commands` 직접 INSERT (권장, 기존 명령과 동일 패턴)

다른 액추에이터 명령과 똑같이 `commands` 테이블에 INSERT. `payload.duration_ms` 만 추가.

```dart
// Flutter
await sb.from('commands').insert({
  'device_id': deviceUuid,              // devices.id (UUID), 본인 소유 → RLS 통과
  'issued_by': sb.auth.currentUser!.id,
  'action': 'mist',
  'payload': {'duration_ms': 10000},    // 앱 칩 5000 | 10000 · 호환 1000/2000/3000/7000 도 허용
});
```

```js
// JS
await sb.from('commands').insert({
  device_id: deviceUuid,
  issued_by: sb.auth.currentUser.id,
  action: 'mist',
  payload: { duration_ms: 2000 },
});
```

- `duration_ms` 허용값: **1000 / 2000 / 3000 / 5000 / 7000 / 10000** (앱 칩은 5000·10000). 기기 상한(`capabilities.mist_max_ms`, 미보고=5000)을 넘는 값은 **서버가 나눠 보낸다** — 10초 = 5초 + 5초 (§1.4).
- 상태 추적은 기존 `commands` Realtime 구독(`commands-rt`) 그대로 — `pending → sent → acked`.

### 1.4 분사 시간 5초/10초 — 10초는 서버가 5초×2 로 분할 (2026-09-23)

앱 칩은 **5초 / 10초** 둘입니다(3·7초 제외 결정, 2026-09-23). 현재 배포된 기기 펌웨어는 분무 1회 상한이
**5초**이고(`MIST_MAX_MS 5000`, 초과는 조용히 clamp) OTA 가 없어 당장 플래시가 어렵습니다.
그래서 **서버가 기기 상한 단위로 나눠 보냅니다**:

```
앱: mist 10000 (행 1개, payload 그대로 저장)
서버 dispatcher: 기기 상한 = capabilities.mist_max_ms (미보고 = 5000)
  → 기기에 mist 5000 발행 → 펌웨어 타이머로 5초 후 OFF
  → 나머지 5000 을 후속 명령으로 예약 (issued_at = 지금 + 5.0s + 1.5s 여유)
  → 약 6.5~7.5초 뒤 mist 5000 발행 → 5초 분사
결과: 5초 분사 · 약 2초 쉼 · 5초 분사 (총 ~12초, 물 양은 10초분)
```

| 항목 | 내용 |
|---|---|
| 왜 분할인가 | 펌웨어 상한을 서버가 넘길 수 없고, 분사마다 **펌웨어 타이머가 끄므로** 후속이 유실돼도 "물이 덜 나옴"일 뿐 펌프가 켜진 채 남지 않음. (`relay_on`/`relay_off` 로 흉내 내면 off 유실 = 침수라 쓰지 않음) |
| 후속 명령 행 | `commands` 에 **별도 행**으로 생김 — `action=mist`, `payload.duration_ms=5000`, `source='timer'`, `reason="mist split of <원 명령 id> (+5000ms)"`, `issued_by`=원 명령과 동일. 명령 이력에서 보이며, 푸시는 나가지 않음(푸시는 `source=schedule` 만) |
| 원 명령 상태 | 첫 분사의 ack 로 `acked`/`ok`. 후속 행은 자기 ack 로 별도 `acked` |
| 앱 버튼 잠금 | **총 분사 시간 + 3초** (10초면 13초) — 후속 분사 중 재요청은 펌웨어가 `busy` |
| 예약(schedule) | 10초 예약도 동일하게 발화 시 분할 |
| 신 펌웨어 | `capabilities.mist_max_ms ≥ 요청` 이면 나누지 않고 한 번에. 기기별로 자동 판단 — 앱 변경 없음 |
| 5초 | 어느 펌웨어에서든 한 번에 |

앱은 `mist_max_ms` 를 볼 필요가 없습니다 — 칩 노출 로직 없이 5초·10초 둘 다 항상 보여주면 됩니다.

### 1.2 발행 — 방법 B: REST 엔드포인트 (서버측 검증 필요 시)

서버가 `duration_ms` 를 화이트리스트 검증 후 큐잉한다. 잘못된 값이면 400.

```
POST /devices/{device_uuid}/mist
Authorization: Bearer <jwt>
Content-Type: application/json

{ "duration_ms": 2000 }
```
```dart
final res = await http.post(
  Uri.parse('https://api.terra-server.uk/devices/$deviceUuid/mist'),
  headers: {'Authorization': 'Bearer $jwt', 'Content-Type': 'application/json'},
  body: jsonEncode({'duration_ms': 2000}),
);
// 201: { "id": "<command uuid>", "action": "mist", "status": "pending" }
// 400: duration_ms 허용값 아님
// 404: 본인 디바이스 아님/미존재
```

> 방법 A(직접 INSERT)와 B(REST)는 결과가 동일하다(둘 다 `commands` 로 들어감). 앱 기존 패턴(직접 INSERT)에
> 맞추려면 A, 서버 검증을 꼭 태우고 싶으면 B. **웹 콘솔은 B 를 사용.**

### 1.3 상태/결과 (acked 시 `result`)

| result | 의미 | UI 처리 |
|--------|------|--------|
| `ok` | 분무 시작(지속시간 뒤 자동 OFF) | ✓ "물분무 N초" |
| `busy` | 이미 분무 중 → 무시됨 | ⏳ "분무 중 — 잠시 후" |
| `bad_request` | duration_ms 누락/0 | ✗ (앱 버그 — 값 확인) |
| `error` | 액추에이터 구동 실패 (GPIO/드라이버 rc≠OK) | ✗ "장치 오류 — 재시도" |
| `unknown_action` | 펌웨어가 `mist` 미지원 | ✗ "디바이스 펌웨어 업데이트 필요" |

> ⚠️ **제어 디바이스(terra-iot-nano) 펌웨어의 `result` 전체 어휘**:
> `ok` / `busy` / `bad_request` / `error` / `locked`(히터 안전잠금) / `unknown_action` /
> `expired`(TTL) / `duplicate`(중복 msg_id). **카메라 펌웨어의 `rejected_*` 계열과 다르다.**
> `locked` 은 히터 계열(`heater_on`/`heater_toggle`)에서만 발생. 위 값 기준으로 처리할 것.

### 1.4 하드웨어 차이는 신경 쓸 것 없음

디바이스가 릴레이든 레벨시프터+MOSFET 이든 **앱 계약은 동일**하다 (`mist` + `duration_ms`).
스위칭 방식 차이는 펌웨어가 흡수한다. 세기(intensity) 조절은 없음 — on/off + 지속시간만.

---

## 2. 예약 타이머 (schedules)

"매일 08:00 물분무 2초", "월·수·금 20:00 팬 켜기" 같은 **정기 명령 예약**. 서버(브리지)가 예약 시각에
`commands` 를 자동 발행한다. 즉 예약은 *명령 발행을 예약*하는 것 — 실제 실행/상태는 §1 과 동일하게
`commands` 로 흐른다.

> 예약 CRUD 는 **REST 전용** (서버가 다음 실행 시각을 계산해서 저장하므로 직접 INSERT 금지).

### 2.1 예약 생성

```
POST /devices/{device_uuid}/schedules
Authorization: Bearer <jwt>
Content-Type: application/json
```

**매일 예약 (물분무 10초, 매일 08:00 KST):**
```json
{
  "action": "mist",
  "payload": { "duration_ms": 10000 },
  "kind": "daily",
  "time_of_day": "08:00"
}
```

**요일 예약 (팬, 월·수·금 20:00 KST):**
```json
{
  "action": "fan_toggle",
  "kind": "weekly",
  "time_of_day": "20:00",
  "days_of_week": [1, 3, 5]
}
```

| 필드 | 값 | 비고 |
|------|-----|------|
| `action` | `mist` \| `fan_toggle` \| `led_on` \| `led_off` \| `relay_toggle` \| `heater_toggle` | 그 외는 400 |
| `payload` | action 인자 | `mist` 는 `{"duration_ms": 5000\|10000}` (호환 1000/2000/3000/7000) 필수. 상한 초과는 발화 시 분할(§1.4) |
| `kind` | `daily` \| `weekly` | |
| `time_of_day` | `"HH:MM"` | **KST(한국시간) 기준** |
| `days_of_week` | `[1..7]` | **weekly 필수.** 1=월 … 7=일 |
| `enabled` | bool | 기본 `true` |

응답(201) = 생성된 예약 1건:
```json
{
  "id": "<uuid>", "device_id": "<uuid>",
  "action": "mist", "payload": {"duration_ms": 2000},
  "kind": "daily", "time_of_day": "08:00:00",
  "days_of_week": null, "enabled": true,
  "next_run_at": "2026-08-11T23:00:00+00:00",   // ← UTC. 표시할 땐 KST 로 변환
  "last_run_at": null, "created_at": "..."
}
```

> **⏰ 시각 주의**: `time_of_day` 는 KST 로 저장/입력하지만, `next_run_at` 은 **UTC** 로 내려온다.
> 화면에 "다음 실행"을 보여줄 땐 KST 로 변환할 것.
> ```dart
> // next_run_at 은 UTC(+00:00). 기기 타임존이 KST 면 toLocal() 한 번으로 KST 벽시계.
> final kst = DateTime.parse(s['next_run_at']).toLocal();
> ```

### 2.2 목록 조회

```
GET /devices/{device_uuid}/schedules
```
→ 해당 디바이스의 예약 배열 (생성 역순).

### 2.3 수정 (부분 업데이트)

```
PATCH /schedules/{schedule_id}
{ "enabled": false }                       // 예약 일시중지
{ "time_of_day": "09:30" }                 // 시각 변경 (next_run_at 서버가 재계산)
{ "kind": "weekly", "days_of_week": [6,7] } // 주말로 변경
```
- `action` 은 수정 불가 (payload 만 변경 가능). 타이밍(kind/time_of_day/days) 변경 시 `next_run_at` 자동 재계산.

### 2.4 삭제

```
DELETE /schedules/{schedule_id}     // 204
```

### 2.5 예약이 실제 실행될 때

예약 시각이 되면 서버가 `commands` 를 INSERT → 앱의 **`commands-rt` 구독에 그대로 잡힌다**
(별도 처리 불필요). 즉 "예약이 방금 실행됨"은 commands Realtime 으로 감지 가능.

### 2.6 (선택) 예약 변경 Realtime 동기화

여러 기기에서 같은 계정을 쓸 때 예약 목록 실시간 동기화:
```dart
sb.channel('schedules-rt')
  .onPostgresChanges(
    event: PostgresChangeEvent.all,
    schema: 'public', table: 'schedules',
    callback: (payload) { /* 목록 갱신 */ },
  ).subscribe();
```

---

## 3. 체크리스트 (앱 구현 시)

- [ ] 분사 시간 칩 **5초 / 10초** → `commands` INSERT `{action:'mist', payload:{duration_ms:5000|10000}}`. 구 펌웨어에서 10초는 5초 두 번(§1.4) — 칩을 숨길 필요 없음
- [ ] 물분무 결과 `busy`/`error`/`unknown_action` UI 처리
- [ ] 예약 생성 폼: 동작 · 반복(매일/요일) · 시각(KST) · (요일 선택 시)요일 · (mist 선택 시)지속시간
- [ ] 예약 목록/수정/삭제 REST 연동
- [ ] `next_run_at`(UTC) → KST 변환 표시
- [ ] 예약 실행 감지는 기존 `commands-rt` 재사용

## 4. SQL 디버깅 (Supabase 대시보드)

```sql
-- 디바이스 예약 목록
SELECT id, action, payload, kind, time_of_day, days_of_week, enabled, next_run_at
FROM schedules WHERE device_id = '<uuid>' ORDER BY created_at DESC;

-- 곧 실행될 예약
SELECT * FROM schedules WHERE enabled ORDER BY next_run_at LIMIT 10;
```
