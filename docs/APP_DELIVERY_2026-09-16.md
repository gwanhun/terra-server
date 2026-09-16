# 앱 전달 — 재설계 연동 최종 현황 (2026-09-16 마감)

> **받는 쪽**: 앱(Flutter)
> **성격**: 9/15~9/16 에 주고받은 요청·회신 전부를 닫는 **최종 현황**입니다. 상세 근거는 부록의 회신 문서에 있고, 이 문서만 보면 "지금 뭐가 되고, 앱이 뭘 하고, 뭐가 남았는지" 를 알 수 있게 썼습니다.
> **운영 상태**: 서버 코드·마이그레이션 전부 배포 완료. 푸시 4종 발송 중. 재설계 SQL 번들 6개 적용 완료·확인 통과.

---

## 0. 한눈에

| 영역 | 상태 | 앱이 할 일 |
|---|---|---|
| 기기 등록 해제 `POST …/unlink` | ✅ 배포 | 이미 붙임. 실검증만 (§3) |
| 푸시 이벤트 4종 (started/ended/failed/skipped) | ✅ 발송 중 · **스테이징 통과** | 실기기 FCM 배달 확인만 (앱) |
| 예약 `payload.brightness` / `duration_ms` | ✅ 지원 | 그대로 |
| 그룹·개체·이력 RPC 6개 + 트리거 | ✅ 운영 적용 | 그대로 붙이기 |
| 그룹 이름 UNIQUE + 409 | ✅ 적용 | RPC `23505` 매핑 (이미 반영) |
| 과거 온습도 유효 표본 수 | ✅ 제공 | 이미 반영 |
| 히터 | ✅ 서버 거절 | 이미 제거 |
| LED 자동 시간 | ⛔ 미지원 (A안 확정) | 이미 제거 |
| `telemetry_1m` / `DELETE /clips` | ⛔ 사용 금지 | 이미 미사용 |

**저희가 앱에서 기다리는 답은 없습니다.** 남은 건 앱의 검증 2건과 §5 의 장기 과제뿐입니다.

---

## 1. 오늘 닫힌 것 — 계약 요약

### 1.1 기기 등록 해제 (소프트)

```http
POST /devices/{uuid}/unlink        POST /cameras/{uuid}/unlink
Authorization: Bearer <JWT>
{ "request_id": "<앱 생성 UUID>" }
→ 200 { "id": "<uuid>", "unlinked_at": "2026-09-16T03:00:00+00:00" }
```

| 상황 | 응답 |
|---|---|
| 정상 / 같은 `request_id` 재시도 / 이미 해제된 기기 | 200 (멱등, 최초 `unlinked_at` 그대로) |
| 미존재·타인 소유 | 404 |
| `request_id` 가 UUID 아님 | 422 |

- 서버: `unlinked_at`, `enclosure_id = NULL`, 예약 비활성, MQTT 계정 회수. **행·센서 기록·명령·알림·클립·R2 전부 보존.**
- 해제 후 목록에서 제외, 단건 `GET`/`PATCH` 와 settings·lcd·mist·schedules 는 404. `unlinked_at` 컬럼은 직결 SELECT 에서 보임.
- `enclosure_id = NULL` UPDATE 가 트리거를 타서 개체↔카메라 이력이 자동 종료됨.
- `DELETE /devices|cameras/{id}` 는 운영용 hard delete 로 남김. 앱 미호출 확인.

### 1.2 푸시 이벤트 — 4종 발송 중

| 항목 | 값 |
|---|---|
| 발행 조건 | `commands.source = 'schedule'` 만 (manual·timer 제외) |
| `started` | 예약 실행 ACK `result == "ok"` |
| `ended` | 구간 예약(`pair_id`)의 `*_off` ACK 만. one-shot 은 `started` 만 |
| `failed` | `result != "ok"` + ACK 없는 3종 (`expired` / `unknown_device` / **`no_ack`** 30초) |
| `skipped` | 가드 스킵. `guard: {kind, metric, threshold, value}` 항상 포함 |
| 공통 필드 | `command_id`, `device_id`(UUID), `device_key`(`terra-…`), `enclosure_id`, `schedule_id`, `execution_source/phase`, `outcome`, `result`, `device_name`, `user_id`, `event_id = command:{id}:{phase}` |
| 재시도 | 5xx·네트워크 지수 백오프, 4xx 무한 재시도 안 함, 같은 `event_id` |

### 1.3 예약 payload

- `payload.brightness`(LED) · `payload.duration_ms`(팬·냉각팬, 상한 2h) — 저장·조회·실행·발행 전 구간 그대로 통과. 서버 검증은 `mist` 뿐.
- 릴레이 보드는 `brightness` 무시하고 켜기만. `brightness: 0` 은 켜기 명령이어도 꺼지므로 **앱 하한 20% 강제** (이미 반영).
- `led_on` + `duration_ms` 는 펌웨어가 무시하고 `ok` 응답 → **미지원 (A안, 칩 제거 완료)**.

### 1.4 재설계 SQL 번들 — 운영 적용 완료

`tera-ai-flutter@bb430fa` 의 6개를 순서대로 적용했습니다. 확인 결과 전부 통과.

| 확인 | 결과 |
|---|---|
| 함수 13개, 시그니처 초안과 동일, `redesign_unlink_device_v1` 없음 | ✅ |
| 트리거 `trg_cameras_touch_assignments` · `trg_pets_touch_assignments` | ✅ |
| `pets.user_id` FK `ON DELETE CASCADE` 단일 (옛 제약 잔존 없음) | ✅ |
| 새 테이블 4 · 새 컬럼 2 | ✅ |
| `authenticated` EXECUTE — RPC 6개 전부 `true` | ✅ |

**함수명·시그니처·오류 코드(`0A000`/`23505`/`40001`/`42501`/`22023`) 초안 그대로입니다.** 앱 변경 없이 붙이면 됩니다. 서버·웹·REST 어느 경로로 관계가 바뀌어도 트리거가 이력을 맞춥니다.

### 1.5 그 외 확정

- **그룹 이름**: `(owner_id, btrim(name))` UNIQUE, 대소문자 구분. REST 도 trim 후 저장. RPC `23505` / REST 409.
- **유효 표본 수**: `telemetry_30m.t_a_count` 등 4컬럼. 9/15 이전 버킷은 `null`. 버킷 경계 UTC.
- **히터**: 예약 400, 즉시 명령 `rejected`/`unsupported_action`.
- **냉각팬**: `telemetry.fan2 != null` 로 노출 (릴레이 보드엔 없음).
- **`busy`/`error`**: 같은 문구, 재시도는 새 `msg_id`.
- **`pets`**: 앱 팀 소유. 서버는 건드리지 않음.

---

## 2. 앱이 이미 반영했다고 확인해주신 것

| 항목 | 앱 버전 |
|---|---|
| `unlink` 호출, 404/405 → "서버 미지원" 표시 | 0.108.1+266 |
| LED "자동 시간" 칩·진행 칩·알림 제거, `brightness` 만 전송 | 0.108.2+267 |
| `t_a_count`/`h_a_count` 사용, null 은 `--`, KST→UTC 조회 | 0.108.3+268 |
| 히터 타일·예약 선택지·구 사육장 탭 타일 제거 | 0.108.4+269 |
| `device.action.skipped` 수신 | 0.108.5+270 |
| 그룹 저장 RPC `23505` → "이미 사용 중인 이름" | — |
| `telemetry_1m` / `DELETE /clips` 미사용, `pets` 앱 소유 유지 | — |

---

## 3. 앱이 할 것

1. ~~**푸시 스테이징 검증 4종**~~ — ✅ **통과 (2026-09-16 07:37 UTC)**. 4종 모두 202 적재, `outcome`/원문 `result`/`device_key`/`guard` 계약 그대로 수신, 같은 `event_id` 재전송은 202 duplicate 로 멱등 처리, 알림 문구 4종 앱 알림 센터 표시 확인. 단말 FCM 배달만 Android 실기기에서 별도 확인 예정(앱).
2. **기기 해제 실검증** — 버려도 되는 기기가 생기면 회신 §8 순서(200 → 목록 제외 → 같은 `request_id` 재호출 200 → 히터 예약 400)로.
3. **RPC 6개 운영 연결** — 초안 그대로라 변경 없음. 붙인 뒤 이상 있으면 함수명·오류코드와 함께 알려주세요.

---

## 4. 저희가 앱에서 기다리는 것

**없습니다.** 9/15 재설계·9/15 푸시·9/16 LED·9/16 결정 답신·9/16 SQL 초안 — 다섯 건 전부 닫혔습니다.

---

## 5. 남은 과제 (장기, 앱 답변 불필요)

| 과제 | 내용 | 담당 | 언제 |
|---|---|---|---|
| **페어링 멱등성** | 같은 기기를 두 번 등록하면 유령 기기가 생김. 서버가 식별자를 매번 새로 만들어서. 펌웨어가 하드웨어 ID(칩 MAC 등)를 페어링 요청에 실어야 함 | 서버 + 펌웨어 | 펌웨어 과제 확정 후. **앱은 BLE 로 받은 값을 그대로 넘기면 될 가능성이 높아 그때 한 줄만 협의** |
| **카메라 알림 저장** | 카메라가 보내는 알림을 서버가 버림 (알림 테이블이 기기만 참조). 관측 coverage 원장의 선행 작업 | 서버 | 스키마 결정 후. petcam-lab coverage 합의와 병행 |
| **LED 타이머 (B안)** | 펌웨어에 LED one-shot 타이머 추가. 생기면 `capabilities.led_timer` 로 알림 | 펌웨어 | 우선순위 낮음. 앱은 플래그 보고 칩 복원 |
| **소유권 이전** | 미설계. 만들 때 `telemetry` 계열에 소유자 컬럼이 없어 과거 센서 기록이 새 소유자에게 보이는 문제를 같이 풀어야 함 | 서버 + 앱 | 앱 기획 나오면 |
| `no_ack` 임계값 | 30초 기본값. 운영 데이터 보고 조정 여지 | 서버 | 실패 알림 빈도 보고 |
| `reconcile` 성능 | owner 전체 재계산. 계정당 카메라 수십 대 넘으면 statement 트리거로 | 앱 SQL | 규모 커지면 |

### 참고 — 운영에서 본 것

- 무응답 처리 도입 직후 과거 누적 **125건**이 `no_ack` 로 정리됐습니다(전체의 약 10%). 대부분 기기가 꺼져 있을 때 발화한 예약으로 추정합니다. 진행형인지 확인 중이며, 진행형이면 사용자가 실패 알림을 자주 받게 되므로 원인을 먼저 잡겠습니다.
- 운영 기기 4대 모두 **B센서 유효 표본이 0** 이라 `t_b_avg` 가 `null` 입니다. A센서만 쓰신다고 하셔서 영향 없지만, 예전엔 이 자리에 가짜 숫자가 나오고 있었습니다.
- 카메라 클립 파이프라인 카운터(`cameras.clip_stats`)가 추가됐습니다. 서버 진단용이라 앱 계약과 무관합니다.

---

## 부록 — 상세 회신 문서

| 문서 | 내용 |
|---|---|
| [BACKEND_HANDOFF_REPLY_REDESIGN_2026-09-15.md](BACKEND_HANDOFF_REPLY_REDESIGN_2026-09-15.md) | 등록·그룹·기록 보존·온습도 계약 실측 (9/15) |
| [BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md](BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md) | 푸시 이벤트 설계 (9/15) |
| [BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md](BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md) | LED 자동 시간·예약 payload·펌웨어 실측 |
| [BACKEND_HANDOFF_REPLY_DECISIONS_2026-09-16.md](BACKEND_HANDOFF_REPLY_DECISIONS_2026-09-16.md) | 결정 답신 7건 반영, `unlink` 계약 확정, skipped |
| [BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md](BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md) | SQL 초안 검토, 트리거, 운영 적용 결과 |
| [API.md](API.md) · [MQTT.md](MQTT.md) · [ENV.md](ENV.md) | 계약 정본 |
