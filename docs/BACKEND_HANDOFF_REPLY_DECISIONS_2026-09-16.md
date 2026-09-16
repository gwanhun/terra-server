# 백엔드 회신 — 9/16 결정 답신 7건 반영 완료 (2026-09-16)

> **회신 대상**: 앱(Flutter) `2026-09-16-lee-gwanhun-reply-2-decisions.md`
> **작성**: terra-server 백엔드 담당
> **성격**: 결정 7건을 **전부 서버에 반영**했습니다. 이 문서는 "결정 → 서버가 한 일 → 앱이 확인할 것" 의 짧은 대조표입니다. 전체 계약은 [APP_DELIVERY_2026-09-16.md](APP_DELIVERY_2026-09-16.md) 가 정본입니다.
> **배포**: 2026-09-16 배포분에 포함. 앱이 이미 붙여둔 `unlink` 호출(0.108.1+266)이 배포 직후부터 200 을 받습니다.

---

## 0. 결정 → 반영 대조

| # | 결정 | 서버 반영 | 앱 확인 |
|:---:|---|---|---|
| 1 | 기기 해제 §1 그대로 진행 | ✅ `POST /devices\|cameras/{uuid}/unlink` 구현. 경로·본문·응답·오류코드 **변경 없음** | 배포 후 404/405 → 200 전환 확인 |
| 2 | secret 별도 채널 | ⏸️ 수령 즉시 활성화 (환경변수 2줄 + 브리지 재시작) | — |
| 3 | LED 타이머 A안 | ✅ 서버·펌웨어 변경 없음 | — |
| 4 | SQL 초안 5개 폴더 전달 | ⏳ 수령. 검토 후 **별도 회신** | — |
| 5 | 히터 서버 400 | ✅ 예약 400 + 즉시 명령 `rejected`/`unsupported_action` | 앱 노출 제거와 정합 |
| 6 | `no_ack` 30초 그대로 | ✅ 기본값 유지 | — |
| 7 | `skipped` §4 계약 | ✅ 구현. **발송 스위치 꺼둠** | "발송 시작" 신호 주시면 켬 |

---

## 1. 기기 해제 — 계약 확정 (요청하신 "함수명·응답·오류코드 확정")

보내주신 §1-1 을 **한 글자도 바꾸지 않고** 구현했습니다.

```http
POST /devices/{uuid}/unlink
POST /cameras/{uuid}/unlink
Authorization: Bearer <사용자 JWT>
Content-Type: application/json

{ "request_id": "<앱 생성 UUID>" }
```

```json
200  { "id": "<uuid>", "unlinked_at": "2026-09-16T03:00:00+00:00" }
```

| 상황 | 응답 | 비고 |
|---|---|---|
| 정상 | 200 | |
| 같은 `request_id` 재시도 | 200, 최초 응답 그대로 | |
| 이미 해제된 기기 + 다른 `request_id` | 200, 기존 `unlinked_at` 그대로 | 재해제 무해 |
| 미존재·타인 소유 | 404 | 기존 소유권 규칙과 동일 |
| `request_id` 가 UUID 형식 아님 / 누락 | **422** | 표에 없던 케이스 — Pydantic 검증 |

`unlinked_at` 은 UTC ISO8601 (`+00:00`) 입니다. 예시의 `+09:00` 과 값은 같고 표기만 다릅니다.

### 서버가 하는 일 (§1-2 순서 그대로)

1. 소유권 확인 (JWT = `owner_id`)
2. `unlinked_at = now()`, `enclosure_id = NULL`, `unlink_request_id` 저장 — **행 삭제 없음**
3. 해당 기기 `schedules.enabled = false` (삭제 없음)
4. 개체↔카메라 이력 종료 → **앱 팀 DB 트리거가 2번 UPDATE 를 잡는 방식에 동의.** 서버는 별도 처리 없음
5. MQTT 계정 회수 (`registry.unregister_device`) — 이후 접속 거부. 이미 붙어 있던 세션이 남아도 브리지가 그 기기 메시지를 **미페어링으로 취급해 버립니다**
6. 멱등 기록 = 2번의 `unlink_request_id`

**보존**: `devices`/`cameras` 행, `telemetry*`, `commands`, `alerts`, `schedules`(비활성), `motion_clips`, `clip_favorites`, R2 원본. §1-2 목록과 동일합니다.

### "한 트랜잭션" 에 대한 답

supabase-py 는 REST 단건 호출이라 2~5 를 한 트랜잭션으로 묶지 못합니다(9/15 회신 §4.3). 대신:

- **2번이 조건부 UPDATE**(`unlinked_at IS NULL` 일 때만)라 동시 호출에도 한 번만 찍힙니다
- **이미 해제된 기기에 재호출해도 3·5 를 다시 실행**합니다. 2번 뒤 어느 단계가 실패해도 기기는 "해제됨" 이고, 재시도 한 번이면 나머지가 정리됩니다
- 앱은 실패 시 **같은 `request_id` 로 재시도**하면 됩니다. 응답은 동일합니다

### 조회 (§1-3)

- `GET /devices` · `GET /cameras`: `unlinked_at IS NULL` 만 반환 ✅
- 단건 `GET`/`PATCH`, 그리고 settings · lcd · mist · schedules 의 기기 경로: 해제된 기기는 **404**
- Supabase 직결 SELECT: `unlinked_at` 컬럼이 그대로 보입니다. RLS 변경 없음 ✅
- `devices`/`cameras` Realtime UPDATE 로 즉시 반영됩니다 ✅

### 남겨두는 것 (§1-4)

- `DELETE /devices|cameras/{id}` hard delete 는 운영·탈퇴용으로 유지. 앱 미호출 확인했습니다
- 재등록 시 새 행 + 옛 행은 `unlinked_at` 상태로 잔존 — 말씀하신 대로입니다. 페어링 멱등성(하드웨어 ID)은 펌웨어 과제와 함께 별도

### 이름 중복 (§1-5)

그룹 이름 UNIQUE 인덱스는 `enclosures` 만이라 영향 없습니다. `redesign_rename_item_v1` 이 `unlinked_at IS NULL` 만 검사하도록 갱신하신 것 확인했습니다.

---

## 2. LED 타이머 — A안 확정

서버·펌웨어 변경 없습니다. `led_on` 은 `brightness` 만 처리하고 `duration_ms` 는 여전히 무시됩니다(칩 제거로 앱에서 더 이상 보내지 않음).
나중에 펌웨어에 타이머가 생기면 `capabilities.led_timer` 로 알리겠습니다.

---

## 3. 히터 — 서버 거절 (§3 요청 반영)

| 경로 | 동작 |
|---|---|
| `POST/PATCH /devices/{id}/schedules` 에 `heater_on`/`heater_off` | **400** `"예약 불가 action"` (허용 목록에서 제거) |
| `commands` 직접 INSERT 로 `heater_on`/`heater_off`/`heater_toggle`/`heater_clear_lock` | 발행하지 않고 `status='rejected'`, `result='unsupported_action'` |

즉시 명령 쪽은 REST 히터 엔드포인트가 원래 없어서(즉시 제어는 앱이 `commands` 직접 INSERT), 발행 단계에서 막았습니다. `commands` Realtime 으로 `rejected` 를 받게 됩니다. 앱이 노출을 제거하셨으니 정상 경로에선 발생하지 않습니다.
히터 보드가 생기면 `capabilities.heater` 플래그로 다시 여는 방식에 동의합니다.

---

## 4. `device.action.skipped` — 구현 완료, 발송 대기

§4 표 그대로입니다.

| 필드 | 값 |
|---|---|
| `type` | `device.action.skipped` |
| `execution_source` | `schedule` |
| `execution_phase` | `skipped` |
| `outcome` | `skipped` |
| `result` | `guard_skipped` |
| `schedule_id` / `action` | 건너뛴 예약 / 명령 |
| `guard` | `{ "kind": "skip_when_temp_above", "threshold": 30, "value": 33.5, "metric": "temperature" }` |
| `event_id` | `command:{command_id}:skipped` (가드 감사 행 id) |
| 공통 | `command_id`, `device_id`(UUID), `device_key`, `enclosure_id`, `device_name`, `user_id` — 기존 3종과 동일 |

`guard.metric` 은 `temperature` | `humidity`, `guard.kind` 는 예약의 가드 타입 문자열 그대로입니다. 숫자는 가공 없이 넘깁니다.

**말씀대로 발송 조건에 넣지 않았습니다.** 서버 스위치(`PUSH_EVENT_SKIPPED_ENABLED`)가 꺼져 있어 적재조차 하지 않습니다. **"skipped 발송 시작해도 됩니다" 신호를 주시면 켭니다** — 브리지 재시작 한 번입니다.

---

## 5. 푸시 — secret 만 남음

- 발행 조건 `source='schedule'`, `outcome`/`result` 분리, `device_id`+`device_key` 병기, `no_ack` 30초 — 앱 검증기와 일치한다고 확인해주신 그대로입니다
- **`PUSH_EVENT_INGEST_SECRET` 수령 → 환경변수 2줄 + 브리지 재시작 → 즉시 발송 시작.** 스테이징 3종(started/ended/failed) 검증은 그 뒤에 같이 하시죠

---

## 6. §5 반영 항목 확인

| 앱 반영 | 서버 확인 |
|---|---|
| `t_a_count`/`h_a_count` 사용, null 은 `--`, KST 범위를 UTC 로 변환 조회 | ✅ 맞습니다. B센서 null 은 A센서만 쓰시니 영향 없음 |
| 그룹 저장은 RPC 라 `23505` 로 처리, REST 409 는 미사용 | ✅ 문제없습니다. REST 409 는 웹 콘솔·직접 호출용으로 남깁니다 |
| 냉각팬 `telemetry.fan2 != null` 노출 | ✅ |
| `busy`/`error` 같은 문구, 재시도는 새 `msg_id` | ✅ |
| `telemetry_1m` / `DELETE /clips` 미사용 | ✅ |
| `pets` 앱 팀 소유 유지 | ✅ 서버는 건드리지 않습니다 |

---

## 7. SQL 초안 5개

`이광헌_전달_2026-09-16` 폴더로 받았습니다. 검토 후 **별도 회신**으로 드립니다. 먼저 답할 수 있는 것:

- **관계 변경 경로 통합(§2-3-2)**: DB 트리거 방식이면 terra-server `PATCH /devices|cameras/{id}` 도, 웹 콘솔 `assignEnclosure()` 도, 이번 `unlink` 의 `enclosure_id = NULL` 도 **전부 자동으로 탑니다.** 서버 코드 변경 없이 커버되므로 초안의 helper 를 트리거에서 호출하는 형태에 동의합니다.
- **`redesign_unlink_device_v1` 연결(§2-3-3)**: 소프트 해제가 REST 로 나갔으니, 앱이 이미 붙인 `POST …/unlink` 를 그대로 쓰시면 됩니다. RPC 자리표시는 `0A000` 그대로 두셔도 되고 제거하셔도 됩니다.

---

## 8. 배포 시 앱 확인 순서

1. `POST /devices/{uuid}/unlink` 가 404/405 대신 **200** 을 주는지 (배포 완료 신호)
2. 해제한 기기가 `GET /devices` 에서 빠지고, `devices` Realtime UPDATE 로 `unlinked_at` 이 오는지
3. 같은 `request_id` 로 한 번 더 호출 → 200 + 같은 `unlinked_at`
4. 히터 예약 생성 시도 → 400 (앱 UI 에선 이미 안 보이지만 계약 확인용)

이상입니다. secret 과 skipped 신호, 두 가지만 주시면 됩니다.
