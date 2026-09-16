# 앱 전달 — 통합 회신 (2026-09-16)

> **받는 쪽**: 앱(Flutter)
> **성격**: 아래 네 건에 대한 **통합 회신**입니다. 근거·코드 위치는 각 상세 문서에 있고, 이 문서는 **앱이 할 일**만 모았습니다.
> - 2026-09-15 재설계 요청서 (기기 등록·그룹·기록 보존·온습도)
> - 2026-09-15 푸시 연동 요청
> - 2026-09-16 LED 자동 시간·예약 payload 확인 요청
> - 2026-09-16 앱 회신 (이름 규칙 확정 + 푸시 6건 답변)

## 배포 상태

| 구분 | 상태 |
|---|---|
| Supabase 마이그레이션 | ✅ 푸시 대기열 · 그룹 이름 UNIQUE · 유효 표본 수 적용 완료. **소프트 해제(`2026-09-16_soft_unlink.sql`)는 배포 시 적용** |
| 서버 코드 | ✅ 9/16 결정 회신 반영분(소프트 해제·히터 차단·skipped 이벤트)까지 구현 완료, 배포 대기 |
| 푸시 이벤트 발송 | ✅ **활성 (2026-09-16 05:52 UTC)** — started/ended/failed/skipped 4종 |

---

## 0. 한눈에

| # | 항목 | 판정 | 앱측 조치 | 우선 |
|---|---|---|---|:---:|
| 1.1 | `telemetry_1m` | ⛔ 영구 빈 테이블 | `telemetry_30m` 로 교체 | ★★ |
| 1.2 | `DELETE /clips/{id}` | ⚠️ 진짜 삭제 | 숨김에 쓰지 말 것 | ★★ |
| 1.3 | 기기 해제 | ✅ **`POST …/unlink` 구현** | 이미 붙인 계약 그대로. `DELETE` 는 계속 미호출 | ★★ |
| 1.4 | `led_on` + `duration_ms` | ⛔ 미지원 (A안 확정) | 칩 제거 완료하심. 서버·펌웨어 변경 없음 | |
| 2.1 | 예약 `payload.brightness` | ✅ 지원 | 그대로. 하한 20% 앱에서 강제 | |
| 2.2 | 예약 `payload.duration_ms` | ✅ 지원 (상한 2h) | 그대로. pair 복귀 불필요 | |
| 2.3 | `PAIR_OK` 로 등록 판정 | ✅ 계약대로 | 이미 적용하심 | |
| 3.1 | 지표별 유효 표본 수 | ✅ 신규 제공 | 가중치를 새 컬럼으로 · B센서 `null` 처리 · KST 는 `+9h` 후 묶기 | |
| 3.2 | 그룹 이름 중복 | ✅ 409 적용 | 409 를 문구로 매핑 | |
| 3.3 | 푸시 이벤트 | ✅ **발송 중** (4종) | 스테이징 4종 검증 | |
| 4.1 | 히터 | ✅ **서버가 거절** | 예약 400 · 즉시 명령 `unsupported_action`. 앱 노출 제거 완료하심 | |
| 4.2 | 냉각팬 | ⚠️ 보드차 | `telemetry.fan2 != null` 로 판별 | |
| 4.3 | `busy` / `error` | ⚠️ 원인 구분 불가 | 같은 문구로 묶기 · 재시도는 **새 `msg_id`** | |

★★ 는 **앱이 잘못 붙은 뒤에는 되돌리기 번거로운 것**입니다. 이것부터 봐주세요.

---

# 1. 지금 바로 고쳐야 하는 것

## 1.1 ⛔ `telemetry_1m` 을 쓰지 마세요

**테이블은 존재하지만 채우는 작업이 없어 영구히 비어 있습니다.** 붙으면 빈 차트가 나옵니다.

저희 문서 3곳(`API.md` / `DATABASE.md` / `ARCHITECTURE.md`)이 이 테이블을 "1년 보관, 1분 평균" 으로 **잘못 안내하고 있었습니다.** 정정 완료했습니다. 그 문서를 보고 구현하셨다면 지금 확인해 주세요.

| 테이블 | 해상도 | 보존 | 용도 |
|---|---|---|---|
| `telemetry` | 3초 | **7일** | 실시간·최근 몇 시간 |
| `telemetry_1m` | — | — | ⛔ 사용 금지 |
| **`telemetry_30m`** | 30분 | 사실상 영구 | **장기 차트 정본** |

## 1.2 ⚠️ `DELETE /clips/{id}` 를 숨김에 쓰지 마세요

**R2 원본 파일과 DB 행을 실제로 삭제**합니다. 복구되지 않습니다.

앱 팀이 사용자별 숨김을 별도 테이블로 처리하신다고 회신 주셨습니다. 그 방침이 맞습니다.

## 1.3 ✅ 기기 등록 해제 — `POST /devices|cameras/{uuid}/unlink` 구현 완료

9/16 결정 회신 §1 의 계약을 **그대로** 구현했습니다. 이미 붙이신 코드(0.108.1+266) 변경 없이 동작합니다.

```http
POST /devices/{uuid}/unlink        (카메라: POST /cameras/{uuid}/unlink)
Authorization: Bearer <JWT>
{ "request_id": "<앱 생성 UUID>" }
```

응답 `200` → `{ "id": "<uuid>", "unlinked_at": "2026-09-16T03:00:00+00:00" }`

| 상황 | 응답 |
|---|---|
| 정상 | 200 |
| 같은 `request_id` 재시도 | 200, 최초 응답 그대로 |
| 이미 해제된 기기 + 다른 `request_id` | 200, 기존 `unlinked_at` 그대로 |
| 미존재·타인 소유 | 404 |
| `request_id` 가 UUID 형식이 아님 | 422 |

**서버 동작** (회신 §1-2 순서 그대로): `unlinked_at = now()`, `enclosure_id = NULL`, 해당 기기 `schedules.enabled = false`, Mosquitto 계정 회수, `unlink_request_id` 저장.
**보존**: 행, `telemetry*`, `commands`, `alerts`, `schedules`(비활성), `motion_clips`, `clip_favorites`, R2 원본.

**해제 후**: `GET /devices`·`GET /cameras` 목록에서 제외. 단건 `GET`/`PATCH` 와 settings·lcd·mist·schedules 호출은 **404**. 브리지도 해제된 기기의 MQTT 메시지를 미페어링으로 취급해 버립니다. `unlinked_at` 컬럼은 앱의 Supabase 직결 SELECT 에서 그대로 보입니다(RLS 변경 없음).

**개체↔카메라 이력 종료**(회신 §1-2-4)는 `enclosure_id = NULL` UPDATE 를 앱 팀 DB 트리거가 잡는 방식에 동의합니다. 서버는 별도로 하지 않습니다.

### 트랜잭션에 대해 (회신 §1-2 "한 트랜잭션")

supabase-py 는 REST 단건 호출이라 한 트랜잭션으로 묶지 못합니다. 대신 **각 단계를 멱등하게** 만들었고, **이미 해제된 기기에 재호출해도 예약 비활성·MQTT 회수를 다시 실행**해 부분 실패를 스스로 메웁니다. 행 표시가 먼저 커밋되므로 그 뒤 어느 단계가 실패해도 기기는 "해제됨" 이고, 재시도 한 번이면 나머지가 정리됩니다. 앱은 실패 시 같은 `request_id` 로 재시도하면 됩니다.

### 이름 중복과 해제 기기 (회신 §1-5)

그룹 이름 UNIQUE 는 `enclosures` 에만 걸려 있어 영향 없습니다. 사육장·카메라 이름은 앱 팀 RPC 담당이며, `unlinked_at IS NULL` 만 검사하도록 갱신하셨다는 것 확인했습니다.

### `DELETE /devices|cameras/{id}` 는 그대로 남깁니다

운영·탈퇴용 hard delete 입니다. 앱은 호출하지 않는 것으로 확인했습니다. 웹 콘솔에는 "해제" 와 "삭제" 를 나란히 두고 문구로 구분했습니다.

### ✅ 함께 확인된 것

- **영상 소유권 격리는 이미 요구대로입니다.** 클립 소유자는 촬영 시점에 행에 박히고 카메라 소유자를 따라가지 않습니다. 재등록한 새 소유자에게 과거 영상이 보이지 않습니다.
- **그룹 삭제는 안전합니다.** 구성원은 보존되고 연결만 끊깁니다.

### ⚠️ 소유권 이전을 만든다면

`telemetry` 계열에는 소유자 컬럼이 없고 기기 소유자를 따라갑니다. 소유권을 넘기면 과거 센서 기록 전체가 새 소유자에게 보입니다. 설계 시 같이 봐주세요.

## 1.4 ⛔ `led_on` + `duration_ms` 미지원

**펌웨어가 `duration_ms` 를 읽지 않습니다. 그냥 켜고 `result: "ok"` 를 보냅니다.**

우려하신 최악의 케이스가 맞습니다. `bad_request` 도 `unknown` 도 오지 않아 **앱도 서버도 "시간이 지나도 안 꺼짐" 을 감지할 수 없습니다.** `telemetry.led` 도 계속 `ON` 입니다.

구조적 이유: 타이머는 릴레이 드라이버의 one-shot 으로 구현돼 있는데 MOSFET 보드의 LED 는 릴레이가 아니라 조광(PWM) 채널이라 **타이머 슬롯이 없습니다.**

**`duration_ms` 를 실제로 처리하는 action 은 `mist` / `fan_on` / `fan2_on` 셋뿐입니다.**

### 타이머 슬롯 질문에 대한 답

- 슬롯은 **액추에이터별로 독립**입니다(펌프·팬1·팬2 각 1개). 기기당 하나가 아닙니다.
- 팬1 타이머가 도는 중에도 팬2 타이머는 정상적으로 걸립니다.
- 다만 **LED 는 슬롯이 아예 없어서** 이 질문 자체가 성립하지 않습니다.

### 결정 부탁드립니다

| 안 | 내용 | 비고 |
|---|---|---|
| A (권장) | 앱이 칩 제거 | 지금 바로 정합. 서버·펌웨어 변경 없음 |
| B | 펌웨어에 LED 타이머 추가 | 릴레이 보드는 한 줄, MOSFET 보드는 새 타이머 구조 필요 |
| C | 서버가 지연 `led_off` 발행 | **권장하지 않음** — 분무 설계 때 의도적으로 배제(OFF 유실 시 안 꺼짐) |

**B 로 가신다면**: ① 지원 플래그 이름(예: `capabilities.led_timer`) ② 상한. **앱의 3시간은 팬 상한 2시간을 넘습니다.** 3시간을 살리려면 펌웨어 상수를 같이 올려야 합니다.

---

# 2. 그대로 써도 되는 것

## 2.1 ✅ 예약 `payload.brightness`

2026-08-12 질문 4("`schedules.payload` 가 `mist` 의 `duration_ms` 외 다른 키도 통과시키는가")의 답은 **통과시킵니다** 입니다.

```jsonc
{ "action": "led_on", "kind": "daily", "time_of_day": "20:00",
  "pair_id": "<uuid>", "payload": { "brightness": 50 } }
```

| 단계 | 동작 |
|---|---|
| POST / PATCH | `payload` 원본 그대로 저장. **`mist` 외에는 검증 안 함** |
| GET | `payload` 그대로 반환 |
| 예약 실행 | `commands.payload` 에 그대로 |
| MQTT 발행 | `led_on` + `brightness` 로 발행 |

- **릴레이 보드는 400 이 아니라 무시입니다.** 서버는 `led_dimmable` 을 확인하지 않고, 릴레이 보드 펌웨어는 `brightness` 를 파싱조차 하지 않고 켜기만 한 뒤 `ok` 를 보냅니다. 기대하신 동작 그대로입니다.
- **⚠️ `brightness: 0` 은 `led_on` 이어도 실제로 꺼집니다**(펌웨어가 `state: "OFF"` 응답). 슬라이더 초기값·복원 로직에서 0 이 새면 "켜기 예약이 실행됐는데 꺼져 있음" 이 됩니다. **하한 20% 를 앱에서 강제해 주세요.** 서버는 막지 않습니다.
- MOSFET 보드는 0~100 으로 조용히 clamp 합니다(하한 없음). 범위 밖이어도 `bad_request` 가 아닙니다.

회귀 테스트를 붙여뒀으니 이 경로가 깨지면 저희 CI 에서 잡힙니다.

## 2.2 ✅ 예약 `payload.duration_ms` (냉각팬)

```jsonc
{ "action": "fan2_on", "kind": "weekly", "time_of_day": "12:00",
  "days_of_week": [6, 7], "payload": { "duration_ms": 1800000 } }
```

**단건 + `duration_ms` 방식으로 진행하세요. pair 로 되돌릴 필요 없습니다.**

- 서버는 §2.1 과 동일 경로로 그대로 전달합니다.
- 펌웨어가 one-shot 타이머를 걸고 시간 뒤 **자동 OFF** 합니다. `fan_on`(환기팬)도 동일합니다.
- **상한은 2시간**입니다. 초과분은 조용히 잘립니다. 앱의 30분/1시간/2시간은 전부 이내입니다.
- ack 의 `state` 가 **`"TIMER"`** 로 옵니다(`duration_ms` 없이 켜면 `"ON"`). 타이머 적용 여부 표시에 쓸 수 있습니다.
- 타이머 진행 중 `fan2_off` 를 보내면 **예약된 자동 OFF 도 함께 취소**됩니다.
- 자동 OFF 후 `telemetry.fan2` 가 **최대 3초 안에** `OFF` 로 바뀝니다.

## 2.3 ✅ `PAIR_OK` 로 등록 완료 판정

이미 적용하셨다고 회신 주셨습니다. 서버측 교차 확인 수단도 있습니다 — `devices` / `cameras` 는 Realtime publication 에 포함돼 있어 INSERT 를 구독할 수 있습니다.

**⚠️ 한 가지**: Mosquitto 계정 등록이 실패해도 서버는 201 + 토큰을 그대로 반환합니다. 즉 `PAIR_OK` 를 받아도 MQTT 접속은 실패할 수 있습니다. **"등록 완료" 와 "온라인" 을 별개로 표시**하는 게 안전합니다.

---

# 3. 새로 제공되는 것

## 3.1 ✅ 과거 온습도 — 지표별 유효 표본 수

요청하신 "정확한 일평균을 위한 유효 표본 수" 입니다. `telemetry_30m` 에 컬럼 4개를 추가해 **적용·배포 완료**했습니다.

```jsonc
sb.from('telemetry_30m')
  .select('bucket, t_a_avg, t_a_min, t_a_max, h_a_avg, sample_count, t_a_count, h_a_count')
```

| 컬럼 | 의미 |
|---|---|
| `sample_count` | 버킷 내 **원본 행 수**. **지표별 유효 표본 수가 아닙니다** |
| `t_a_count` / `h_a_count` | A센서 온도·습도의 **유효 표본 수** (센서 정상 보고분만) |
| `t_b_count` / `h_b_count` | B센서 온도·습도의 유효 표본 수 |

**가중평균은 `sample_count` 가 아니라 지표별 `*_count` 를 가중치로 쓰세요.**
요청하신 `(20×1 + 30×3) / 4 = 27.5` 가 나오려면 그래야 합니다. 온도 센서만 죽어도 `sample_count` 는 그대로이기 때문입니다.

### ⚠️ 세 가지 주의

1. **소급 불가.** 원본이 7일만 보존되므로 **2026-09-15 이전 버킷의 `*_count` 는 `null`** 입니다. 요청서 방침대로 `--` 로 처리해 주세요.
2. **센서 고장 값이 이제 제외됩니다.** 예전에는 고장 보고 시의 무의미한 값이 평균·최소·최대에 섞여 들어갔습니다. **즉 지금까지 "그럴듯한 숫자" 로 보이던 값이 가짜였을 수 있습니다.** 실측하니 운영 중인 기기 4대 모두 **B센서 유효 표본이 0 이고 `t_b_avg` 가 `null`** 로 나옵니다. B센서를 화면에 쓰신다면 `null` 처리를 확인해 주세요.
3. **일 단위 묶을 때 타임존.** 버킷 경계는 **UTC 기준** 정시/30분입니다. KST 일평균은 `bucket + 9h` 로 날짜를 만든 뒤 묶어야 합니다. 그대로 자르면 9시간 어긋납니다.

`APP_TIMESERIES_CHART.md` 의 "`sample_count < 300` 이면 부분 데이터" 규약은 위 1번 때문에 아직 정확하지 않습니다. 새 컬럼이 충분히 쌓인 뒤 지표별 기준으로 바꾸는 걸 권합니다.

## 3.2 ✅ 그룹 이름 중복 금지 (409)

보내주신 비교 규칙 그대로 DB 제약을 만들고 적용했습니다.

- 인덱스 식: **`(owner_id, btrim(name))`** — `lower()` 를 쓰지 않아 **대소문자를 구분**합니다
- 범위: **그룹 이름은 `enclosures` 안에서만** 유일
- `POST` / `PATCH /enclosures` 가 위반 시 **409** 를 돌려줍니다. 기존에는 500 이었습니다
- **길이 10자와 허용 문자 검증은 서버에서 하지 않습니다.** PostgreSQL `char_length` 가 grapheme 수가 아니라 부정확해서입니다. 앱과 앱 팀 RPC 가 담당하는 게 맞습니다
- 사육장·카메라 이름은 두 테이블 합산이라 단일 인덱스로 불가능합니다. `redesign_rename_item_v1` 에서 검사하는 설계에 동의합니다

**인덱스 생성이 성공했으므로 기존 중복 이름은 없었습니다.** 사전 정리가 불필요했습니다.

## 3.3 ✅ 푸시 이벤트 — 회신 6건 전부 반영

### 확정 계약

| 항목 | 확정 내용 |
|---|---|
| 발행 조건 | **`source = 'schedule'` 단 하나.** manual·guard·timer 전부 제외 |
| `device.action.started` | 예약 실행 ACK 가 `result == "ok"` 일 때 |
| `device.action.ended` | **구간 예약(`pair_id`)의 `*_off` ACK 에서만.** one-shot 은 `started` 만 (A안) |
| `device.action.failed` | `result != "ok"` + **ACK 없이 끝난 3종**(아래) |
| `execution_source` | `schedule` 만 옵니다 |
| `execution_phase` | `started` / `ended` / `failed` |
| `outcome` | `succeeded` / `failed` (앱 판정값) |
| `result` | 펌웨어 원문 또는 서버 값 |
| `device_id` | UUID (조인 키) |
| `device_key` | `terra-xxxxxxxx` (MQTT client_id) |
| `schedule_id` | `commands.source_id` |
| `event_id` | `command:{command_id}:{phase}` — 재시도 시 동일 값 |

### ACK 없이 끝나는 3종 — 전부 `failed`

| `result` | 언제 |
|---|---|
| `expired` | TTL 초과로 발행조차 못 함 |
| `unknown_device` | 미등록 기기 |
| **`no_ack`** | 발행했는데 **30초 안에 응답 없음** |

`no_ack` 가 요청하신 "sent 30초 만료" 입니다. 발행 스레드가 10초 주기로 훑어 `commands.status` 를 `no_ack` 로 바꾸고 실패 이벤트를 적재합니다. 그 사이 ACK 가 들어오면 **조건부 UPDATE 라 덮어쓰지 않습니다** — 정상 ACK 가 우선입니다.

> 30초는 저희 기본값입니다. 다르게 원하시면 알려주세요, 설정값입니다.

### 전송 동작

- 대기열에 적재 후 워커가 5초 주기로 POST
- 네트워크 오류·5xx 는 지수 백오프 재시도, **4xx 는 무한 재시도하지 않음**
- secret 은 **헤더로만** 나갑니다. 대기열 본문·로그에 들어가지 않습니다

### 남은 것

**`PUSH_EVENT_INGEST_SECRET` 만 주시면 켭니다.** 값이 없으면 워커가 시작조차 하지 않습니다.

### `device.action.skipped` — 회신 §4 계약대로 구현, **발송은 꺼둠**

| 필드 | 값 |
|---|---|
| `type` | `device.action.skipped` |
| `execution_source` / `execution_phase` / `outcome` | `schedule` / `skipped` / `skipped` |
| `result` | `guard_skipped` |
| `schedule_id` · `action` | 건너뛴 예약 · 명령 |
| `guard` | `{ "kind", "threshold", "value", "metric" }` — 문구 재료 (`metric` 은 `temperature` \| `humidity`) |
| `event_id` | `command:{command_id}:skipped` (가드 감사 행 id) |

**2026-09-16 "발송 시작해도 됩니다" 신호(앱 0.108.5+270) 받았습니다. 스위치 켰습니다.** secret 이 들어오면 기존 3종과 함께 나갑니다.

---

# 4. 보드·펌웨어 제약

## 4.1 ✅ 히터 — 서버가 거절합니다 (요청 §3 반영)

두 보드 모두 펌웨어에서 히터가 미구현(핸들 NULL)이라 항상 `unknown_action` 이 돌아왔습니다. 요청대로 **서버가 발행 전에 막습니다.**

| 경로 | 동작 |
|---|---|
| `POST/PATCH /schedules` 에 `heater_on`/`heater_off` | **400** (예약 허용 목록에서 제거) |
| `commands` 직접 INSERT 로 `heater_*` | 발행하지 않고 `status=rejected`, `result=unsupported_action` |

앱에서 히터 노출을 제거하셨으니 정상 경로에선 발생하지 않습니다. 히터 보드가 생기면 `capabilities.heater` 플래그로 다시 여는 방식에 동의합니다.

## 4.2 ⚠️ 냉각팬 UI 노출 조건

**릴레이 보드에는 `fan2_*` 명령 코드 자체가 없습니다.** 보내면 `unknown_action` 입니다.
서버는 `fan2_*` 예약 생성을 허용하므로 **예약은 만들어지고 실행만 실패**합니다.

→ 기존 계약대로 **`telemetry.fan2 != null`** 로 노출 여부를 판별해 주세요 (`APP_FAN2_2026-09-07.md` §4).

## 4.3 ⚠️ `busy` / `error` 문구와 재시도

`busy` 는 두 원인에서 나오고 **구분되지 않습니다.**
- 같은 액추에이터에 **이미 타이머가 걸려 있는데** 또 타이머 명령
- **직전 명령 후 200ms 이내** (최소 토글 간격 가드)

게다가 같은 200ms 가드 위반이 **비타이머 명령(`fan_off`, `led_*`, `relay_on` 등)에서는 `error`** 로 나옵니다.
→ 둘 다 "잠시 후 다시 시도" 계열로 묶는 게 안전합니다.

**재시도는 새 명령으로.** 펌웨어가 최근 `msg_id` 8개를 기억해 중복을 거르는데 **`busy`·`error` 로 거절된 명령도 기억합니다.** 같은 `msg_id` 로 재시도하면 `duplicate` 만 돌아옵니다.

## 4.4 참고 — ack 가 아예 없는 경우

명령 JSON 이 깨지면 펌웨어가 ack 를 보내지 않습니다. 정상 경로에선 발생하지 않지만, `commands` Realtime 을 지켜본다면 `sent` 로 남는 케이스가 있었습니다. **이제 30초 뒤 `no_ack` 로 정리됩니다**(§3.3).

---

# 5. 정정 — 개체(`pets`)

저희가 9/15 회신에서 `pets` 와 `assign_pet_to_enclosure` 를 "없음" 으로 적었는데, 그건 **terra-server 저장소 기준**이었습니다. 알려주신 대로 **같은 Supabase 프로젝트의 `public` 스키마에는 이미 있고 앱이 사용 중**인 걸 확인했습니다.

**terra-server 는 `pets` 를 새로 만들거나 덮어쓰지 않습니다.** 앱 팀 소유 객체로 취급하겠습니다. `behavior_logs` / `behavior_labels` 를 petcam-lab 소유로 두는 것과 같은 방식입니다.

---

# 6. 전달주신 SQL 초안

`codex/figma-redesign-20260915` 브랜치에서 초안 5개·검증기·계약서를 읽었습니다. **검토 회신**: [BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md](BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md) — 배포 전 수정 5건(트리거 부재·카메라 FK RESTRICT·`user_id` ON DELETE 누락·해제 기기 필터·검증기 스냅샷), 권장 4건, 적용 순서 제안.

먼저 답할 수 있는 것 하나. **관계 변경 경로 통합은 DB 트리거 방식이면 서버 코드 변경이 필요 없습니다.**
트리거는 DB 레벨이라 terra-server 의 `PATCH /devices|cameras/{id}` 도, 웹 콘솔의 `assignEnclosure()` 도, 앱의 직접 UPDATE 도 **전부 자동으로 탑니다.** 저희가 트리거를 권한 이유가 그것입니다. 초안의 helper 를 트리거에서 호출하는 형태에 동의합니다.

`redesign_unlink_device_v1` 연결 경로는 소프트 해제(§1.3) 설계가 끝나면 알려드리겠습니다.

---

# 7. 저희가 기다리는 답

**저희가 기다리는 답은 없습니다.** 9/16 결정 회신 7건, skipped 신호, secret 까지 전부 반영됐고 푸시는 발송 중입니다.

앱 쪽에서 진행하실 것:
- 스테이징 4종(started / ended / failed / skipped) 수신 검증 → 결과 공유
- 기기 해제 실검증 (버려도 되는 기기가 생기면)

SQL 초안 5개는 전달해주신 폴더로 검토하겠습니다. 결과는 별도 회신으로 드립니다.

---

# 8. 서버측 남은 작업 (참고)

| 작업 | 상태 |
|---|---|
| 기록을 남기는 기기 해제 | ✅ 완료 (§1.3) — 마이그레이션 적용 + 배포만 남음 |
| 이름 중복 제약 + 409 | ✅ 완료 (§3.2) |
| 지표별 유효 표본 수 | ✅ 완료 (§3.1) |
| 푸시 이벤트 | ✅ 발송 중 (§3.3) |
| 페어링 멱등성 | 앱·펌웨어 협의 필요 — 같은 기기를 두 번 등록하면 **유령 기기**가 생깁니다 |
| 개체↔카메라 이력 | ✅ 검토 회신 + 앱 답 5건 수신·트리거 버그 수정 반영. **반영본 브랜치 push 대기 → 저희가 운영 적용** |
| 카메라 알림 저장 | 스키마 결정 후. 현재 카메라가 보낸 알림은 서버가 버립니다 |

---

# 부록 — 상세 회신 문서

근거와 코드 위치가 필요하시면 아래를 보세요.

| 문서 | 다루는 절 |
|---|---|
| [BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md](BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md) | §1.4 · §2.1 · §2.2 · §4 |
| [BACKEND_HANDOFF_REPLY_REDESIGN_2026-09-15.md](BACKEND_HANDOFF_REPLY_REDESIGN_2026-09-15.md) | §1.3 · §3.1 · §3.2 · §5 |
| [BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md](BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md) | §3.3 |
| [APP_TIMESERIES_CHART.md](APP_TIMESERIES_CHART.md) | 시계열 차트 계약 (§3.1) |
| [APP_FAN2_2026-09-07.md](APP_FAN2_2026-09-07.md) | 냉각팬 계약 (§2.2 · §4.2) |
| [MQTT.md](MQTT.md) | action 목록·지원 여부·보드차 |
