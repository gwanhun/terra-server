# 백엔드 회신 — 예약/타이머 실행 푸시 이벤트 연동 (2026-09-15)

> **회신 대상**: 앱(Flutter) `2026-09-15-terra-server-slack-script.md` (Android 알림 연동 요청)
> **작성**: terra-server 백엔드 담당
> **성격**: 코드 실측 대조 + 설계 회신. 요청하신 확인 5건에 답하고, 계약 조정이 필요한 3건을 제기합니다.
> **결론 먼저**: **`device.action.started` 와 `device.action.failed` 는 구현 가능**합니다.
> **`device.action.ended` 는 현재 서버 구조로 관측할 수 없습니다** — §3 에서 선택지를 제시합니다.

---

## 0. 요약

| # | 확인 요청 | 판정 |
|---|---|---|
| 1 | ACK 최종 확정 코드 위치 | ✅ **확정** — `backend/mqtt/handlers.py:411-422` 직후 |
| 2 | `user_id`/`command_id`/`device_id`/`schedule_id` 확보 | ✅ **추가 조회 없이 가능**. 단 **컬럼 이름이 문서와 다름** |
| 3 | outbox·재시도 위치 | ⚠️ **기존 구현 없음(그린필드)**. 브리지 프로세스 내 신설 권장 |
| 4 | payload 필드와 기존 용어 충돌 | ⚠️ **이름 충돌 0건, 값 충돌 2건** (`source` 의 `timer`, `result` 어휘) |
| 5 | 구현·스테이징 가능 시점 | §7 참고 — **`ended` 범위 확정이 선행** |

추가로 저희가 제기하는 것:

| # | 항목 | 내용 |
|---|---|---|
| A | **`ended` 관측 불가** | 서버에 명령 종료 개념이 없음. 펌웨어 one-shot 타이머라 종료 ACK 가 안 옴 (§3) |
| B | **성공/실패 판정 기준** | `status` 는 결과와 무관하게 항상 `acked`. **`result` 문자열로 갈라야 함** (§4) |
| C | **`execution_source` 값 정의** | `timer` 라는 값이 서버에서 한 번도 쓰인 적 없음. 정의 필요 (§5) |

> **인프라 확인**: 요청서에 적힌 Supabase 프로젝트는 terra-server 가 쓰는 것과 **동일한 프로젝트**입니다
> (`.env` 의 `SUPABASE_URL` 과 일치). 즉 Edge Function 과 terra-server 가 같은 DB 를 봅니다 — §6 에서 이 점을 활용한 대안을 제시합니다.

---

## 1. 확인 1 — ACK 가 최종 확정되는 코드 위치 ✅

**함수**: `handle_ack()` — `backend/mqtt/handlers.py:376`
**확정 지점**: `commands` UPDATE — `backend/mqtt/handlers.py:411-422`

```python
res = (
    sb.table("commands")
    .update({"status": "acked", "result": result, "acked_at": _now_iso()})
    .eq("id", msg_id)
    .eq("device_id", device_uuid)
    .execute()
)
```

- 호출 경로: 펌웨어 `esp32/{device_id}/ack` publish → paho 콜백(`backend/mqtt/bridge.py`) → `handle_ack`
- 실행 프로세스: **`terra-bridge.service`** (API 서버가 아닙니다). 진입점 `backend/mqtt_bridge_main.py`
- MQTT `msg_id` 는 `commands.id` 를 그대로 씁니다 (`migrations/2026-05-26_initial_schema.sql:112`)
- 카메라 ack 는 `commands` 를 안 거치고 `last_seen` 만 갱신하고 return 합니다 (`handlers.py:391-400`) — 푸시 대상 아님

**→ 이벤트 발행은 위 UPDATE 직후가 가장 자연스럽습니다.** 이유는 §2 에 이어집니다.

---

## 2. 확인 2 — 이벤트 생성 시 필드 확보 ✅ (이름 조정 필요)

### 2.1 추가 조회 없이 나오는 것

`postgrest-py` 의 `update()` 는 기본이 `Prefer: return=representation` 이라, 위 `res.data[0]` 에 **갱신된 행 전체**가 들어옵니다. **JOIN 불필요합니다.**

현재 코드는 이 값을 존재 확인에만 쓰고 버립니다 (`handlers.py:427`) — 그래서 여기가 발행 지점으로 최적입니다.

| 요청서 필드 | terra-server 원천 | 추가 조회 |
|---|---|---|
| `command_id` | `commands.id` | 불필요 |
| `user_id` | **`commands.issued_by`** (`user_id` 컬럼은 없음) | 불필요 |
| `schedule_id` | **`commands.source_id`** (`schedule_id` 컬럼은 없음) | 불필요 |
| `execution_source` | `commands.source` | 불필요 |
| `action` | `commands.action` | 불필요 |
| `result` | `commands.result` | 불필요 |
| `device_id` | MQTT 토픽의 TEXT id / 행의 UUID **둘 다 있음** | 불필요 |
| `enclosure_id` | `devices.enclosure_id` | **필요** |
| `device_name` | `devices.name` | **필요** |

### 2.2 `user_id` 보장 수준

`commands.issued_by` 는 **스키마상 NULL 허용**입니다 (`initial_schema.sql:114`). 실제로는 거의 항상 채워집니다.

| 발행 경로 | `issued_by` |
|---|---|
| REST 분무 | 요청자 JWT — `backend/routers/commands.py:103` |
| LCD | 요청자 JWT — `backend/routers/lcd.py:109` |
| **예약 실행** | `schedules.owner_id` — `backend/schedule_runner.py:132` |
| 앱 직접 INSERT | **RLS 가 `issued_by = auth.uid()` 를 강제** — `initial_schema.sql:213` |

**→ 그래도 방어가 필요합니다.** NULL 이면 `devices.owner_id` 로 폴백하겠습니다 (`initial_schema.sql:21`, NOT NULL 이라 항상 존재).

### 2.3 `device_id` 표기 확정 요청 ⚠️

요청서 예시가 `"device_id": "terra-device-id"`(TEXT)인데 같은 payload 의 `enclosure_id` 는 UUID 입니다. 혼재하면 앱에서 조인이 깨집니다.

**제안**: 둘 다 싣겠습니다.
- `device_id` — UUID (다른 필드와 일관, Supabase 조인 키)
- `device_key` — `terra-xxxxxxxx` TEXT (MQTT client_id, 사람이 읽는 식별자)

다른 이름이 좋으면 알려주세요.

### 2.4 구현 메모 (저희 작업)

브리지는 별도 프로세스라 `devices` 조회 캐시를 따로 들고 있습니다. 현재 캐시는 id 매핑만 담습니다 (`handlers.py:66-103`).
**`device_uuid → {owner_id, name, enclosure_id}` lru_cache 헬퍼를 추가**해 조회 2건을 없애겠습니다. 기존 패턴과 동일합니다.

> 주의: 기기 삭제·이름 변경 시 캐시 무효화가 현재 미구현입니다 (`handlers.py:26-27`). 이름이 바뀌면 최대 캐시 수명만큼 옛 이름이 알림에 나갈 수 있습니다. TTL 을 짧게 잡겠습니다.

---

## 3. ⛔ `device.action.ended` — 현재 구조로 관측 불가

**요청서에서 유일하게 막히는 항목입니다.**

### 3.1 근거

- `commands` 에 `started_at` / `ended_at` / `duration` 컬럼이 **없습니다** (`initial_schema.sql:111-125` + `migrations/2026-08-12_commands_source_and_guard.sql:12-15` 전수 확인).
- **지속시간 명령의 자동 OFF 는 펌웨어 내부 one-shot 타이머가 처리합니다.** 종료를 알리는 두 번째 ACK 가 없습니다.
  - 분무: `docs/APP_TIMER_MIST.md:15-16` — "펌웨어가 내부 타이머로 자동 OFF. **OFF 명령을 따로 보내면 안 됨**"
  - 팬: `docs/MQTT.md:99` — `fan_on` + `duration_ms`, 펌웨어가 최대 2시간으로 clamp. 서버는 검증도 추적도 없이 passthrough
- `acked_at` 은 "명령을 받아 **실행을 시작**했다"에 해당합니다. 종료 추적 테이블도, 서버 타이머도 없습니다.

### 3.2 지금 낼 수 있는 `ended` 는 하나뿐입니다

**구간 예약**은 on/off 가 `pair_id` 로 묶인 **별도 2행**이라(`migrations/2026-08-18_schedules_pair_id.sql:10`, `backend/routers/schedules.py:82-84`), off 명령의 ACK 가 진짜 종료입니다. 이건 §1 훅으로 그대로 발행됩니다.

반대로 **분무 2초, 팬 `duration_ms`** 같은 one-shot 은 종료를 서버가 전혀 모릅니다.

### 3.3 선택지

| 안 | 내용 | 정확도 | 작업량 |
|---|---|---|---|
| **A (권장)** | `ended` 를 **구간 예약의 off 명령에만** 한정. one-shot 은 `started` 만 발행 | 정확 | 없음 (§1 훅으로 커버) |
| B | `acked_at + payload.duration_ms` 로 지연 이벤트 스케줄러 신설 | 추정치 (실제 조기 종료·실패 반영 못 함) | 중 |
| C | telemetry 의 `fan`/`relay`/`fan2` 상태 전이 감지 (`handlers.py:339-341`) | 실측이나 15초 지연·유실 가능 | 중~대 |

**A 로 시작하고, 사용자 피드백을 보고 B/C 를 검토하는 걸 권합니다.** B·C 는 모두 신규 메커니즘이라 일정이 늘어납니다.

**앱 팀 확인 요청**: `ended` 를 A 범위로 좁혀도 알림 UX 가 성립하는지 알려주세요. 이 답에 따라 §7 일정이 정해집니다.

---

## 4. ⚠️ 성공/실패 판정 기준 — 요청서 가정과 다릅니다

### 4.1 `status` 로는 실패를 못 가릅니다

`handle_ack` 는 **응답 내용과 무관하게 항상 `status='acked'`** 를 씁니다 (`handlers.py:415`).
`result` 가 `busy`, `error`, `unknown_action`, `rejected_locked` 여도 `acked` 입니다. 코드베이스에 `status='failed'` 를 쓰는 곳이 없습니다.

존재하는 `status` 값 전부:

| 값 | 세팅 위치 |
|---|---|
| `pending` | `backend/command_service.py:71` |
| `sent` | `backend/mqtt/dispatcher.py:129` |
| `acked` | `backend/mqtt/handlers.py:415` |
| `expired` (TTL 초과, 발행 안 됨) | `dispatcher.py:97` |
| `rejected` (미등록 기기) | `dispatcher.py:105` |
| `skipped` (가드 스킵) | `backend/schedule_runner.py:96` |

### 4.2 `result` 어휘가 두 체계로 섞여 있습니다

| 출처 | 값 |
|---|---|
| 제어 기기 펌웨어 (`docs/APP_TIMER_MIST.md:80-83`) | `ok` / `busy` / `bad_request` / `error` / `locked` / `unknown_action` / `expired` / `duplicate` |
| MQTT 명세 카메라 계열 (`docs/MQTT.md:128-133`) | `ok` / `rejected_locked` / `rejected_ttl_expired` / `rejected_unknown_action` / `rejected_duplicate_msg_id` |
| 서버가 직접 기록 | `unknown_device` (`dispatcher.py:105`) / `guard_skipped` (`schedule_runner.py:97`) |

`docs/APP_TIMER_MIST.md:82` 가 두 체계가 다르다고 명시합니다. `result` 는 자유 TEXT 이고 CHECK 제약이 없습니다.

### 4.3 제안하는 판정 규칙

- **성공**: `result == "ok"` **만**
- **실패**: 그 외 전부 → `device.action.failed`
- `busy` / `unknown_action` 은 실패지만 사용자 문구를 달리할 수 있게 `result` 원문을 payload 에 그대로 싣겠습니다

### 4.4 ACK 가 아예 안 오는 경우 ⚠️

TTL 만료(`expired`)·미등록 기기(`rejected`)는 **발행 단계에서 끝나므로 §1 훅을 타지 않습니다.** 별도 발행 지점이 필요합니다 (`dispatcher.py:97`, `:104-106`).

다만 현재 dispatcher 의 SELECT 는 `id, device_id, action, payload, issued_at, ttl_sec` 만 가져옵니다 (`dispatcher.py:65`). `issued_by`/`source`/`source_id` 를 쓰려면 **SELECT 컬럼 확장**이 필요합니다. 저희가 처리하겠습니다.

**그리고 응답이 영영 안 오는 경우(기기 오프라인, ACK 유실)는 현재 어떤 상태로도 남지 않습니다.** `sent` 인 채로 방치됩니다. 실패 알림을 원하시면 "sent 상태로 N초 경과 시 실패 처리" 로직을 새로 만들어야 합니다. 필요 여부를 알려주세요.

---

## 5. ⚠️ payload 용어 — 이름 충돌 없음, 값 충돌 2건

### 5.1 이름은 안전합니다

`execution_source`, `execution_phase` 는 `backend/`·`migrations/`·`docs/`·`specs/`·`web/` 전체에 **0건**입니다. 신규 용어로 써도 됩니다.

### 5.2 ⚠️ 충돌 1 — `execution_source` 의 `timer`

`commands.source` 컬럼이 이미 있고 CHECK 제약 값이 정해져 있습니다 (`migrations/2026-08-12_commands_source_and_guard.sql:25`).

| 값 | 실제 사용 |
|---|---|
| `manual` | ✅ REST 분무·LCD·앱 직접 INSERT 전부 (DB DEFAULT) |
| `schedule` | ✅ 예약 러너 — `schedule_runner.py:134` |
| `guard` | ✅ 가드 스킵 감사 기록 — `schedule_runner.py:98` |
| **`timer`** | ⛔ **코드 어디에서도 세팅된 적 없음** (CHECK 제약과 docstring 에만 존재) |

요청서는 "예약 또는 타이머로 실행된 명령"이라 했는데, **지속시간이 붙은 즉시 제어(분무 2초 등)는 서버에서 `manual` 로 들어옵니다.** 타이머가 아닙니다.

**확인 요청**: `timer` 로 무엇을 지칭하는지 알려주세요. 세 가지 중 하나일 텐데 처리 방식이 다릅니다.
1. 지속시간이 붙은 즉시 제어 → 현재 `manual`. 요청서의 "즉시 제어 제외" 방침과 충돌
2. 구간 예약(on/off 쌍) → 현재 `schedule`
3. 미구현 개념 → 이번엔 안 보냄

### 5.3 ⚠️ 충돌 2 — `result` 라는 이름

`commands.result` 가 이미 존재하고 값 어휘가 §4.2 대로입니다. 요청서는 같은 이름에 `succeeded`/`failed` 라는 **다른 어휘**를 씁니다.

**제안**: 둘 다 싣고 이름을 분리합니다.
- `outcome` — `succeeded` / `failed` (요청서의 판정값)
- `result` — 펌웨어 원문 (`ok`/`busy`/`unknown_action`/…)

### 5.4 `action` 값은 일치합니다

`fan_on` 등 요청서 예시가 실제 값과 맞습니다. 전체 목록은 `docs/MQTT.md:97-105` 가 정본입니다.
**예약 가능한 action 은 더 좁습니다** — toggle 계열이 의도적으로 제외돼 있습니다 (`backend/routers/schedules.py:48-55`). 예약 발 알림에는 toggle 이 나오지 않습니다.

### 5.5 발행 범위

요청서 방침("즉시 제어 제외")을 그대로 적용하면 훅에서 `source in ('schedule', 'guard')` 만 발행하면 됩니다. 가드 스킵(`guard`)도 사용자에게 알릴지 알려주세요. 현재는 "온도가 높아 난방 예약을 건너뜀" 같은 감사 기록이 남습니다 (`schedule_runner.py:91-101`).

---

## 6. 확인 3 — outbox / 재시도 위치

### 6.1 기존 구현 없음 (그린필드)

- `outbox`, `retry_queue`, `dead_letter` — 코드·SQL·문서 전체 **0건**
- 재시도라 부를 만한 건 둘 다 DB 폴링 기반입니다
  - 발행 실패 시 `pending` 유지 → 1초 뒤 재시도 (`dispatcher.py:123-126`)
  - 카메라 회전 재발행, 60초 최소 간격 (`handlers.py:215-232`)
- **외부 HTTP 호출 코드가 없습니다.** stdlib `urllib` 로 JWKS 조회(`backend/auth.py:22, 67`), `boto3` 로 R2(`backend/r2_client.py`) 뿐입니다
- `httpx` 는 **이미 의존성에 있습니다** (`pyproject.toml:36`). 추가 설치 불필요합니다
- 푸시 관련 테이블·코드도 전무하며 미착수 스펙으로만 존재합니다 (`specs/stage-h-timer-mist-push.md:3, 108-142`)

### 6.2 제안 A — 브리지 내 outbox 테이블 (권장)

```
handle_ack UPDATE 직후 → push_outbox INSERT (같은 DB, 같은 커밋 흐름)
                        → 별도 워커 스레드가 폴링하며 Edge Function 에 POST
```

- 기존 `CommandDispatcher`·`ScheduleRunner`·`OfflineMonitor` 와 **동일한 폴링 스레드 패턴**이라 일관됩니다 (`backend/mqtt_bridge_main.py:44-45`)
- MQTT 수신 스레드를 HTTP 지연으로 막지 않습니다
- 재시도·지수 백오프·`event_id` 멱등키를 outbox 행에 담습니다
- **단일 인스턴스 가정**은 기존과 동일합니다 (`terra-bridge.service` 1개)

### 6.3 제안 B — Postgres 트리거 (같은 프로젝트라 가능)

요청서의 Supabase 프로젝트가 terra-server 와 **동일**하므로, `commands` UPDATE 트리거에서 `pg_net` 으로 Edge Function 을 부르는 방법도 가능합니다.

- 장점: 서버 코드 변경 최소, 경로 무관하게 보장(앱 직접 INSERT 포함)
- 단점: `device_name`·`enclosure_id` 조인을 SQL 로 해야 하고, 재시도·관측이 DB 안에 갇힘. `pg_net` 확장 활성화 필요

**저희는 A 를 권합니다.** 재시도와 로그를 서버 쪽에서 다루는 게 운영상 낫습니다. 다만 앱 팀이 B 를 선호하면 트리거 SQL 작성은 앱 팀, 검토·적용은 저희가 맡는 §2-2 와 같은 분장으로 갈 수 있습니다.

### 6.4 보안 준수 사항

- `PUSH_EVENT_INGEST_SECRET` 은 **문서·커밋에 절대 넣지 않고** 서버 `.env` 에만 둡니다 (`chmod 600` 유지). 별도 안전 채널로 받겠습니다
- **⚠️ 로그 주의**: 현재 일부 경고 로그가 payload 를 그대로 찍습니다 (예: `handlers.py:458`). 푸시 경로에서는 secret 과 요청 본문을 로그에 남기지 않도록 하겠습니다
- `event_id` 는 요청서 규칙대로 `command:{command_id}:{phase}` 로 만들어 재시도 시 동일 값을 씁니다

---

## 7. 확인 5 — 구현·스테이징 시점

`ended` 범위(§3.3)와 `timer` 정의(§5.2)가 확정돼야 규모가 정해집니다. 확정 후 순서는 이렇습니다.

| 단계 | 내용 | 선행 조건 |
|---|---|---|
| 1 | `push_outbox` 마이그레이션 + 브리지 워커 스레드 | 없음 |
| 2 | `handle_ack` 훅 + 기기 메타 캐시 (§1, §2.4) | 없음 |
| 3 | dispatcher 만료/거부 훅 + SELECT 확장 (§4.4) | 없음 |
| 4 | `ended` 처리 | **§3.3 선택 확정** |
| 5 | 스테이징 3종 검증 + 중복 재전송 검증 | secret 전달 |

### 앱 팀에 확인 부탁드리는 것

1. **`ended` 를 구간 예약에만 한정해도 되는지** (§3.3) — 가장 중요합니다
2. **`execution_source` 의 `timer` 정의** (§5.2)
3. **`device_id` 표기**: UUID + TEXT 병기안 수용 여부 (§2.3)
4. **`outcome` / `result` 이름 분리** 수용 여부 (§5.3)
5. **가드 스킵(`guard`)도 알릴지** (§5.5)
6. **응답 없음(오프라인·ACK 유실) 실패 알림이 필요한지** (§4.4)

### 온습도 안전 알림 (`safety.*`) 사전 메모

요청서대로 이번엔 보내지 않지만, 발행 지점은 이미 깔끔합니다.

- 발생: `_insert_alert()` — `backend/alerts.py:85-100`
- 해제: `_resolve_alert()` — `backend/alerts.py:103-113`
- **cooldown 을 따로 만들 필요가 없습니다.** 같은 `(device_id, kind)` 의 미해제 알림이 있으면 INSERT 를 건너뛰므로(`alerts.py:88`), 해제 전까지 알림이 1회만 생성됩니다. 해제 조건에는 히스테리시스가 걸려 있습니다 (`alerts.py:43-44`)
- **⚠️ 주의 2가지**
  - 펌웨어가 직접 보낸 알림은 dedup 없이 무조건 INSERT 됩니다 (`handlers.py:471`). 여기 푸시를 붙이면 중복 폭주 위험이 있습니다
  - `alerts` 에 소유자 컬럼이 없어(`initial_schema.sql:139-155`) 대상 사용자는 `devices.owner_id` 조회가 필요합니다. service_role 사용 시 **`.eq("owner_id", ...)` 명시 필터 필수**입니다
