# Stage R — 자동 환기·자동 분무 Implementation Plan

> **구현 방식 (CAOF):** Critical 트랙. 이 계획을 task 단위로 구현한다. Steps use checkbox (`- [ ]`) syntax for tracking.
> 상태: r3 (2026-11-05) — r2 확정(교차 검수 반영, 사용자 승인) 뒤 Figma 확인으로 습도 원인 변경(아래 변경 기록).
>
> **r3 변경 (사용자 결정 2026-11-05, Figma `1657:5406` 디자이너 메모)**: "습도 {Max}% 초과 1시간 이상 유지(습도 주의 이벤트와 같은 조건)에 환기를 시작". 그래서 `humid_high` 연속 시간 = **3600초**(`env_rules.OVER_HUMID_ENTRY_SEC`)로 바꾸고, 분무 뒤 60분 보호(`POST_MIST_GUARD_SEC`·R3 습기 신호·`wet_mono`)는 **삭제**한다. 온도 원인(180초)·분무(180초)·추운 날 금지는 그대로. R6 타이머 만료 재발행도 R4 와 같이 원인별 연속 시간(습도 3600초·온도 180초)을 다시 요구한다 — 만료 시점에 MAX 이하(적정 범위)면 재발행 없이 종료. 아래 Task 본문의 `wet_mono`·`test_post_mist_guard` 등은 r2 구현 기록이며 r3 에서 코드·테스트 모두 제거됐다.

**Goal:** 사용자가 켠 기기에서, 브리지가 사용자 적정 온습도(MIN/MAX)를 기준으로 환기팬을 자동으로 켜고 끄고, 습도가 MIN 아래로 내려가면 자동으로 1회 분무한다.

**Architecture:** v4 알림과 같은 자리(브리지 `alerts._evaluate_v4`, 샘플마다)에서 순수 상태 기계 `backend/auto_env.py` 가 판정하고, DB 글루 `backend/auto_env_store.py` 가 `commands` 에 `source='auto'` 명령을 INSERT 한다. 발행은 기존 디스패처(1초 폴링)가 한다. 환기팬은 항상 `fan_on` + `duration_ms`(1시간)로 켜서 서버가 멈춰도 펌웨어가 스스로 끈다. 펌웨어 변경은 없다. 처음엔 **섀도 모드**(의도만 로그, 명령 없음)로 배포한다.

**Tech Stack:** Python 3.12, paho-mqtt 브리지, Supabase(Postgres·RLS·Realtime), pytest(`tests/fake_sb.py`).

**Spec:** 앱 요청서 `/Users/baek/myProjects/tera-ai-flutter/docs/handoffs/2026-11-05-auto-vent-mist-request.md`(§3 초안 숫자 = 확정값), 검수 `reports/stage-r-auto-env-review-final.md`(메인 체크아웃), 펌웨어 타이머 회신 `docs/BACKEND_HANDOFF_REPLY_LED_TIMER_2026-09-16.md` §3.2·§3.3, v4 판정 `specs/stage-n-env-alerts-v4.md`, 습기 사이클 `specs/stage-o-humid-cycle-v4.md`.

## Global Constraints

- 사용자 결정(2026-11-05):
  - 숫자는 앱 초안 그대로. 우리 안전값 4개(종료 확인 60초·재발행 최소 5분·자동 환기 끝난 뒤 최소 10분 쉼·하루 자동 환기 12시간 상한)도 채택.
  - 고온에도 **환기팬(`fan`)만** 켠다(`fan2` 안 씀).
  - v4 알림 푸시는 **그대로** 둔다.
  - "예약 실행 중" = **구간 예약 켜짐 구간만**.
  - **추운 날 환기 멈춤**: 온도 ≤ 온도 MIN 이면 습도 원인 환기를 시작하지 않고, 돌고 있으면 끈다.
  - 환기·분무 둘 다 이번에 구현한다. 실제 명령은 이관훈님 펌웨어 확인 + 섀도 모드 뒤에 켠다.
- 상수 (`backend/auto_env.py`, 이 표가 정본):

| 상수 | 값 | 출처 |
|---|---|---|
| `HOLD_SEC` | 180 — 온도 원인(`>` MAX)·분무(`<` MIN)가 연속 180초 이상이면 성립(`elapsed >= 180`) | 앱 §3-1·§3-2 |
| `HUMID_HOLD_SEC` | 3600 — 습도 원인(`>` MAX) 연속 1시간(= `env_rules.OVER_HUMID_ENTRY_SEC`, v4 과습 주의와 같은 조건) | Figma 메모(r3) |
| `VENT_HUMID_MARGIN` | 5.0 — 습도 종료선 = `max(MAX−5, (MIN+MAX)/2)` (MAX−MIN 이 5면 종료선이 MIN 에 닿아 분무와 핑퐁하므로 중간값 하한) | 앱 §3-1 + 검수 |
| `VENT_TEMP_MARGIN` | 0.5 — 온도 종료선 = MAX−0.5 | 앱 §3-1 |
| `END_HOLD_SEC` | 60 — 종료선 이하 연속 60초 | 우리 안전값 |
| `VENT_DURATION_MS` | 3_600_000 | 앱 §3-1 |
| `REFIRE_MIN_SEC` | 300 — 직전 자동 명령 뒤 이 시간 안엔 재발행 안 함 | 우리 안전값 |
| `VENT_MIN_OFF_SEC` | 600 — 자동 환기 종료 뒤 다음 자동 시작까지 | 우리 안전값 |
| `VENT_DAILY_MAX_SEC` | 43_200 — KST 하루 자동 환기 누적(자동 소유 시간) 12시간 | 우리 안전값 |
| `MANUAL_PAUSE_SEC` | 1800 | 앱 §3-3 |
| `MIST_DURATION_MS` | 3000 | 앱 §3-2 |
| `MIST_MIN_INTERVAL_SEC` | 7200 — **모든 source** 마지막 분무(ACK ok) 기준 | 앱 §3-2 "직전 분무" |
| `MIST_DAILY_MAX` | 4 — KST 하루 **자동** 분무 시도 수 | 앱 §3-2 |
| `GAP_SEC` | 60 — `env_rules.GAP_SEC` 재사용 | v4 D2 |
| `OFF_STREAK` | 2 — 텔레메트리 `fan=='OFF'` 연속 샘플 수 | 우리 결정 |
| `EXPIRY_SLACK_SEC` | 30 — `last_on + 3600 − 30` 이후의 OFF 는 타이머 만료로 봄 | 우리 결정 |
| `RETRY_SEC` | 30 — 환기 명령 INSERT 실패 재시도 간격 | 우리 결정 |
| `STATE_RETRY_SEC` | 60 — `auto_env_state` 쓰기 실패 재시도 | 우리 결정 |

- 서버 스위치:
  - `AUTO_ENV_ENABLED`: 기본 꺼짐.
  - `AUTO_ENV_DRY_RUN`: 기본 **켜짐**. 켜져 있으면 INSERT 대신 `logger.info("auto-env intent ...")` 만 한다.
  - `AUTO_ENV_DEVICES`: 쉼표 구분, 비면 전체.
  - 판정은 `_evaluate_v4` 안에서 돌기 때문에 **`ALERTS_V4_ENABLED` 가 선행 조건**이다. v4 를 끄면 자동도 멈추고, 켜져 있던 팬은 펌웨어 타이머로 꺼진다.
  - 기기별 토글: `device_settings.auto_vent_enabled` / `auto_mist_enabled`(기본 false).
- 시간:
  - 경과·연속 판정은 단조 시간(`alerts._now_mono()`, v4 D7 과 같음)으로 한다.
  - 저장 시각과 KST 날짜는 wall 시간으로 한다.
- 명령:
  - `insert_pending_command(..., source="auto", reason=..., issued_by=owner_id)` 로 넣는다(예약·복원과 같은 `issued_by`). `retain=False` 는 기존 디스패처 규칙이다.
  - 자동 명령은 푸시 없음(`PUSH_EVENT_SOURCES` 기본 `schedule` 만, 실패 푸시도 같은 필터).
- 비용: 토글 두 컬럼은 `alerts._thresholds` 의 기존 SELECT 에 추가한다(추가 쿼리 0). DB 호출은 전이·복원·ACK 때만 한다. 토글 둘 다 꺼진 기기는 자동 경로 비용 0.
- 프로젝트 규칙: `uv add` 만, bare `except` 금지, service_role 쿼리는 `device_id` 명시 필터, 비밀값 커밋 금지.
- 건드리지 않는 것: v4 알림 판정·푸시 규칙(`env_rules.py`), 습기 사이클 판정(`humid_cycle.py`), 예약 실행기(`schedule_runner.py`)·재부팅 복원(`schedule_restore.py`), 펌웨어, 일일·주간 분석.

## 판정 규칙 (결정 고정)

| # | 규칙 |
|---|---|
| R1 | 입력은 v4 와 같은 샘플이다. `t_a`·`h_a`(`a_ok` 가 거짓이거나 숫자가 아니면 값 없음), `fan`·`relay`(키 존재 여부 포함). 값이 없거나 직전 샘플과 간격이 60초를 넘으면 연속 타이머를 초기화하고 명령을 내지 않는다. 기준은 `alerts._thresholds` 의 `Thresholds`(기본 18~27℃ / 40~80%). 기준이 바뀌면 `_thresholds` 가 v4 트래커와 함께 자동 트래커 타이머도 초기화한다. |
| R2 | 환기 원인은 2개다. `temp_high` = 온도 > MAX 연속 180초. `humid_high` = 습도 > MAX **이고 온도 > 온도 MIN**(온도 값 없으면 성립 안 함)이 연속 **3600초**(r3, Figma). 분무 직후 높은 습도는 보통 1시간 안에 내려가므로 따로 보호하지 않는다. |
| R3 | (r3 삭제) 분무 뒤 60분 보호·습기 신호. 모든 source 의 `mist` ACK ok 는 R9 분무 간격(2시간)에만 쓴다. |
| R4 | 시작 조건은 아래가 모두 맞을 때다. 환기 토글 켜짐, 텔레메트리에 `fan` 키 있음, 원인 ≥ 1, 수동 쉼 중 아님, 구간 예약 켜짐 중 아님, 마지막 자동 종료 뒤 600초 지남, 오늘 누적 < 12시간, 텔레메트리 `fan == 'OFF'`. 이때 `fan_on {duration_ms: 3600000}` 을 낸다(reason 은 아래 표). `fan == 'ON'`(사용자·예약이 켬)이면 명령 없음. 소유는 **자동 명령의 ACK `result='ok'`** 때 확정하고, 다른 결과(`busy`·`error`·`expired`·`no_ack`)면 소유 없음 + `REFIRE_MIN_SEC` 뒤 재판정. |
| R5 | 종료: 서 있던 원인이 모두 종료선 이하로 연속 60초면 `fan_off` 를 낸다(reason `auto_vent_end`). 온도 ≤ MIN 이 되면 습도 원인은 즉시 해제한다. 그 결과 원인이 0이면 바로 `fan_off`(reason `auto_vent_cold`). 오늘 누적이 12시간에 닿으면 `fan_off`(reason `auto_vent_cap`). 셋 다 **자동 소유일 때만** 낸다. 종료 시각을 저장해 R4 의 600초 쉼에 쓴다. |
| R6 | 자동 소유 중 텔레메트리 `fan == 'OFF'` 가 2샘플 연속으로 보이면 두 경우로 나눈다. ⓐ `now ≥ last_on + 3600 − 30`(타이머 만료)이면 원인이 남아 있을 때 R4 조건(600초 쉼 제외)으로 재발행한다. 이건 이어서 켜는 것이다. ⓑ 그보다 이르면 외부 끔(사용자 끔 ACK 유실·로컬 조작·재부팅)으로 보고 소유 해제 + 30분 쉼. 두 경우 모두 직전 자동 명령 뒤 300초 안이면 기다린다. **타이머 중 재발행은 하지 않는다.** 타이머가 걸린 팬에 타이머 명령을 다시 보내면 펌웨어가 `busy` 로 거절한다(§3.3(2)). |
| R7 | 자동이 아닌 팬 명령의 ACK `result='ok'` 처리. 수동 `fan_off` → 소유 해제 + 30분 쉼. 수동 `fan_toggle` → 자동 소유 중이면 `fan_off` 와 같고, 아니면 무시. 수동 `fan_on` → 소유 해제만. `schedule`·`restore` 의 `fan_*` → 소유 해제만. 위 경우 모두 그 기기의 `source='auto'`·`status='pending'` 팬 명령을 조건부 UPDATE 로 `status='expired', result='superseded'` 로 만든다. 디스패처는 `source='auto'` 행을 발행 직전 다시 읽어 pending 이 아니면 보내지 않는다(코드 리뷰 2026-11-05). 이미 나간 자동 명령과의 경합(수백 ms)은 남는다. |
| R8 | 구간 예약 켜짐 중의 판정. 대상은 같은 `pair_id` 의 `fan_on`·`fan_off` 두 행이 **둘 다 `enabled`** 인 pair 다. 각 pair 의 `compute_prev_run` 이 더 늦은 행이 `fan_on` 이면 그 pair 는 켜짐 중이다. pair 하나라도 켜짐 중이면 "예약 중"(합집합). 예약 중에는 시작·종료·재발행을 하지 않고 소유를 해제한다. 예약 목록은 기기별 60초 캐시로 두고, 명령 INSERT 직전에는 다시 읽는다. 예약이 끝난 뒤(fan_off) 원인이 남아 있으면 3분 뒤 자동이 다시 켤 수 있다. 의도된 동작이고 앱 회신에 적는다. 단발 예약·`skip_when_*` 으로 건너뛴 회차는 고려하지 않는다(앱 회신에 명시). |
| R9 | 자동 분무 조건은 아래가 모두 맞을 때다. 분무 토글 켜짐, 텔레메트리에 `relay` 키 있음, 습도 < MIN 연속 180초, 무장(armed) 상태, `fan == 'OFF'`, 같은 step 에서 `fan_on` 을 내지 않음, 모든 source 의 마지막 분무 뒤 2시간 지남, 오늘 자동 분무 < 4. 이때 `mist {duration_ms: 3000}`(reason `auto_mist`)을 낸다. **시도 순간에 예산을 쓴다**: 카운트 +1, 마지막 분무 시각 = 지금, 무장 해제. INSERT 결과와 무관하고 재시도하지 않는다. 습도 ≥ MIN 샘플이 오면 다시 무장한다. 팬이 켜져 있으면 보류한다(연속 타이머는 유지). |
| R10 | 토글 끔은 60초 캐시 안에 반영된다. 자동 환기를 끄면 자동 소유일 때 `fan_off`(reason `auto_vent_disabled`)를 내고 환기 상태를 초기화한다. 자동 분무를 끄면 분무 상태만 초기화한다. `AUTO_ENV_ENABLED` 끔·허용목록에서 빠짐·기기 해제는 게이트 분기에서 상태만 버린다(명령 없음, 켜진 팬은 펌웨어 타이머로 꺼짐). |
| R11 | 브리지 재시작 복원은 토글이 하나라도 켜진 기기에서 첫 샘플 때 1회 한다. ① `devices.auto_env_state` 에서 `paused_until`·`last_end_at`·`runtime_today_sec`·`mist.today_count`·`day` 를 읽는다. ② 정합 확인 쿼리 2개를 낸다. 최근 1시간 그 기기의 `fan_*` 명령 전부(`issued_at` 내림차순, status·result·source)로 **마지막 작성자가 자동 `fan_on` 이고 ACK ok 이며 1시간 안**일 때만 소유를 복원한다. KST 오늘 0시부터의 `mist` 명령(source 무관 ACK ok 마지막 시각 + 자동 시도 수)으로 분무 간격·하루 수를 다시 계산하고, ①과 다르면 큰 쪽을 쓴다. 조회가 실패하면 이 샘플은 판정 보류(다음 샘플 재시도). |
| R12 | 상태 노출(C3): `devices.auto_env_state` 를 **`updated_at` 을 뺀 내용이 바뀔 때만** UPDATE 하고 `.eq("id", device_uuid)` 를 명시한다. 실패하면 60초 뒤 재시도한다. 모양: `{"vent": {"active", "causes", "since", "paused_until", "schedule_hold", "last_end_at", "runtime_today_sec"}, "mist": {"last_at", "today_count"}, "day": "YYYY-MM-DD"(KST), "updated_at"}`. 앱은 읽기만 하고, 써도 브리지가 덮어쓴다. |
| R13 | 환기 명령 INSERT 실패(`insert_pending_command` → None/예외)면 상태를 바꾸지 않고 30초 뒤 같은 판정으로 재시도한다. 분무는 R9 대로 재시도하지 않는다. 섀도 모드에서는 INSERT 대신 로그를 남기고, 소유는 "즉시 ACK ok" 로 보고 모의 팬 상태를 R6 입력으로 쓴다(실제 팬은 안 바뀌므로). |

### reason 값 (C2)

| reason | 뜻 |
|---|---|
| `auto_vent:humid_high` / `auto_vent:temp_high` / `auto_vent:humid_high+temp_high` | 자동 환기 시작·재발행(원인) |
| `auto_vent_end` | 원인 해소로 종료 |
| `auto_vent_cold` | 온도 ≤ MIN 으로 습도 환기 중단 |
| `auto_vent_cap` | 하루 12시간 상한 |
| `auto_vent_disabled` | 사용자가 자동 환기 토글 끔 |
| `auto_mist` | 자동 분무(습도 < MIN) |

## 앱 계약 (요청서 §4)

| # | 서버 |
|---|---|
| C1 | `device_settings.auto_vent_enabled`·`auto_mist_enabled`(bool NOT NULL DEFAULT false). `GET/PATCH /devices/{id}/settings` 에 포함. PATCH 는 `true`/`false` 만 받는다(null·문자열은 422). |
| C2 | `commands.source = 'auto'`, `reason` 은 위 표. `issued_by` = 기기 소유자. |
| C3 | `devices.auto_env_state`(R12). `devices` 는 이미 Realtime 대상. |
| C4 | 별도 capability 없음. 환기는 텔레메트리 `fan` 키, 분무는 `relay` 키가 있는 기기만. |
| C5 | `source='auto'` 라 예약 목록·예약 결과와 안 섞임. |

## File Structure

| 파일 | 책임 |
|---|---|
| Create `backend/auto_env.py` | 순수 상태 기계. 단조 시간·값 입력 → 명령 의도 출력, DB·시계 모름 |
| Create `backend/auto_env_store.py` | 예약 캐시, 재시작 복원, 명령 INSERT·섀도 로그, pending 무효화, `auto_env_state` 쓰기, ACK 처리, 스위치 |
| Create `migrations/2026-11-05_auto_env.sql` | 컬럼 3개 + `commands_source_check` 에 `'auto'` |
| Modify `backend/alerts.py` | `_DeviceState.auto` 필드(상태는 여기 한 곳), `_thresholds` SELECT 2컬럼·타이머 초기화, `_evaluate_v4` 게이트·호출, `on_command_ack(row)` 공개 함수(`_v4_lock` 안에서 `_states` 접근) |
| Modify `backend/mqtt/handlers.py` | `_ack_device_command` 의 UPDATE 성공 뒤 `alerts.on_command_ack(row)`, `try/except Exception` 으로 격리 |
| Modify `backend/routers/settings.py` | C1 |
| Tests | `tests/test_auto_env.py`, `tests/test_auto_env_store.py`, `tests/test_settings_api.py`, `tests/test_alerts.py`, `tests/test_mqtt_handlers.py` |
| Docs | `docs/APP_AUTO_ENV_2026-11-05.md`, `docs/ENV.md`, `docs/API.md`, `docs/DATABASE.md`, `MIGRATIONS_APPLIED.md`(적용 후) |

## Review Focus

1. **MAX 근처 플래핑** → Task 1 `test_no_flap_near_max`: 습도 MAX+1 180초 → on. MAX−1 10분 → off 없음. 종료선 아래 60초 → off 1회. 그 뒤 MAX+1 이 다시 와도 600초 안엔 on 없음.
2. **사용자가 끈 직후·끔 ACK 유실** → Task 1 `test_manual_off_pauses_30min`, `test_external_off_before_expiry_pauses`.
3. **물통이 빔·센서 고장** → Task 1 `test_mist_disarms_until_recovery`(습도 30% 고정 24시간 → 1회), `test_mist_cap_with_recovery`(회복·하강 반복 → 하루 4회, 간격 ≥ 2시간, KST 0시 리셋), `test_vent_daily_cap`(습도 99 고정 → 누적 12시간에 `auto_vent_cap`, 그날 재시작 없음).
4. **재시작 뒤 남의 팬을 자동 소유로 오인** → Task 3 `test_restore_last_writer`.
5. **예약·사용자 타이머와 겹침** → Task 1 `test_schedule_hold_blocks_all`, Task 3 `test_schedule_span_pairs_union`, `test_manual_ack_supersedes_pending_auto`.

---

### Task 1: 순수 상태 기계 `backend/auto_env.py`

**Files:** Create `backend/auto_env.py`, `tests/test_auto_env.py`

**Context:**
- Depends on: 없음
- Produces:
  - `@dataclass(frozen=True) class AutoInput: mono: float; wall: datetime; temp: float | None; humid: float | None; fan: str | None; has_fan: bool; has_relay: bool; th: env_rules.Thresholds; vent_on: bool; mist_on: bool; schedule_hold: bool; wet_mono: float | None`
  - `@dataclass(frozen=True) class AutoCmd: action: Literal["fan_on","fan_off","mist"]; payload: dict[str, Any]; reason: str`
  - `class AutoEnvTracker`:
    - `step(inp: AutoInput) -> list[AutoCmd]`
    - `issued(cmd: AutoCmd, mono: float) -> None` — INSERT 성공(또는 섀도). 환기는 "요청 중", 분무는 R9 예산을 `step` 에서 이미 썼으므로 no-op
    - `insert_failed(cmd, mono) -> None`
    - `on_own_ack(action: str, ok: bool, mono: float) -> None` — 자동 명령 ACK
    - `on_other_ack(action: str, source: str, mono: float) -> None` — R7
    - `on_mist_ok(mono: float) -> None` — 모든 source 분무 ACK ok(R3·R9 간격)
    - `reset_timers() -> None` — 기준 변경
    - `restore(...)` — R11 값들
    - `state_json(wall: datetime) -> dict` — R12
  - 모든 상수는 모듈 상단(Global Constraints 표).
- Must know:
  - 하루 경계는 `inp.wall.astimezone(KST).date()` 다. 날이 바뀌면 `runtime_today_sec`·`mist_today` 를 0으로.
  - 누적 시간은 소유 중 샘플 간격을 더한다(간격 > 60초면 60초까지만).
  - 같은 의도를 다시 낼 때의 간격은 R13 의 30초다.
  - 시계 역행(mono 감소)은 없지만 wall 역행은 날짜 계산에만 영향을 준다.
- Acceptance: `uv run pytest tests/test_auto_env.py -q` 통과.

- [ ] **Step 1: 실패 테스트** — 헬퍼 `run(tr, secs, step=3, **sample) -> list[AutoCmd]`, `t0 = 2026-11-05 12:00 KST`, 기본 th(18~27 / 40~80).
  - `test_humid_high_starts_after_hold`: 습도 85, 177초까지 `[]`. 180초째 샘플에서 `[AutoCmd("fan_on", {"duration_ms": 3600000}, "auto_vent:humid_high")]`.
  - `test_post_mist_guard`: `on_mist_ok(t0)` 뒤 습도 85 → 60분+180초 전엔 `[]`. 온도 30 이면 가드와 무관하게 `auto_vent:temp_high`.
  - `test_cold_blocks_humid_vent`: 습도 85·온도 17.5(MIN 18 이하) → `[]`. 소유 중(습도 원인) 온도 18 이하가 되면 → `fan_off` reason `auto_vent_cold`.
  - `test_no_flap_near_max`: (Review 1).
  - `test_humid_end_line_midpoint`: th 습도 75~80 → 종료선 77.5(=max(75, 77.5)). 76 에서 60초 → off.
  - `test_temp_cause_keeps_running`: 두 원인 → 습도 70 이어도 온도 28 이면 유지. 온도 26.4 60초 → `auto_vent_end`.
  - `test_fan_on_by_user_no_command`: fan 'ON' + 습도 85 → 명령 없음. 이후 습도 60 → `fan_off` 없음.
  - `test_ownership_needs_ack`: `issued` 만 하고 ACK 없음 → 소유 아님(원인 해소 시 `fan_off` 없음). `on_own_ack("fan_on", True)` 뒤에만 `fan_off`. `ok=False` → 300초 뒤 재판정 fan_on.
  - `test_manual_off_pauses_30min`: 소유 중 `on_other_ack("fan_off","manual")` → 30분 동안 `[]`, 30분+180초 뒤 fan_on. `fan_toggle` 도 소유 중이면 같음, 비소유면 쉼 없음.
  - `test_expiry_refire`: 소유 후 3570초 시점부터 fan 'OFF' 2샘플 → fan_on 재발행(같은 reason), 600초 쉼 적용 안 함.
  - `test_external_off_before_expiry_pauses`: 소유 후 20분에 fan 'OFF' 2샘플 → 명령 없음, 소유 해제, 30분 쉼.
  - `test_schedule_hold_blocks_all`: `schedule_hold=True` → 시작·종료·재발행 없음, `state_json()["vent"]["schedule_hold"] is True`, 소유 해제.
  - `test_vent_daily_cap`: (Review 3).
  - `test_mist_low_humid`: 습도 35·fan 'OFF' 180초 → `[AutoCmd("mist", {"duration_ms": 3000}, "auto_mist")]`.
  - `test_mist_disarms_until_recovery`, `test_mist_cap_with_recovery`: (Review 3).
  - `test_mist_interval_counts_any_source`: `on_mist_ok`(수동) 1시간 뒤 습도 35 → `[]`, 2시간 뒤 mist.
  - `test_mist_needs_fan_off_known`: fan None 또는 `has_relay=False` → `[]`. fan 'ON' 10분 → `[]`, 'OFF' 첫 샘플 → mist.
  - `test_fan_on_and_mist_same_step`: 온도 30 + 습도 35 동시 → `fan_on` 만, mist 없음.
  - `test_no_fan_key_no_vent`: `has_fan=False` → 환기 명령 없음.
  - `test_missing_or_gap_resets`: 습도 85 120초 → None 1샘플 → 85 120초 → `[]`. 70초 간격도 같음.
  - `test_threshold_change_resets`: 120초 지점 `reset_timers()` → 180초 시점 `[]`.
  - `test_toggle_off_sends_off_when_owned`: 소유 중 `vent_on=False` → `auto_vent_disabled` 1회.
  - `test_insert_failed_retry_30s`: `insert_failed` → 30초 안 `[]`, 30초 뒤 같은 fan_on. 분무 `insert_failed` → 재출력 없음.
  - `test_kst_day_rollover`: 23:59 KST 자동 분무 4회 소진 → 00:00 KST 뒤 다시 가능.
- [ ] **Step 2:** `uv run pytest tests/test_auto_env.py -q` → ImportError 확인.
- [ ] **Step 3: 구현.** 원인별 `(over_since, under_since)` 와 분무 `low_since/armed/last_mist/today` 를 둔다. 환기는 `phase ∈ {idle, requested, owned}`, `last_on`, `paused_until`, `last_end`, `runtime`, `off_streak`, `retry_after`. 모두 mono 기준.
- [ ] **Step 4:** 통과 확인.
- [ ] **Step 5: 커밋** `feat(auto-env): 자동 환기·분무 순수 상태 기계`

### Task 2: 마이그레이션 + 설정 API

**Files:** Create `migrations/2026-11-05_auto_env.sql` · Modify `backend/routers/settings.py`, `tests/test_settings_api.py`

**Context:**
- Depends on: 없음
- Produces:
  - `SettingsUpdate.auto_vent_enabled: StrictBool | None = None`, `auto_mist_enabled` 같음. 명시적 null 은 validator 로 422. 생략은 "안 바꿈".
  - `SettingsOut` 두 필드 `bool`(행 없음 → False).
  - `_SETTINGS_COLS` 에 두 컬럼 추가.
- Must know: PATCH 는 `exclude_unset` upsert 라 두 필드만 보내면 온습도 값이 그대로 남아야 한다. 운영 적용은 Chrome Supabase SQL 편집기로 **코드 배포보다 먼저** 한다(기본 false 라 동작 불변).
- Acceptance: `uv run pytest tests/test_settings_api.py -q` 통과. 운영 적용 뒤 `MIGRATIONS_APPLIED.md` 에 기록.

```sql
-- 2026-11-05: 자동 환기·자동 분무 (specs/stage-r-auto-env.md, 앱 요청 2026-11-05 C1~C3)
ALTER TABLE public.device_settings
    ADD COLUMN IF NOT EXISTS auto_vent_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS auto_mist_enabled BOOLEAN NOT NULL DEFAULT false;

-- 브리지가 쓰는 현재 상태(R12). 앱은 읽기만 한다(기존 devices RLS·Realtime 그대로 — 소유자 쓰기는 막지 않고 브리지가 덮어씀).
ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS auto_env_state JSONB;

ALTER TABLE public.commands DROP CONSTRAINT IF EXISTS commands_source_check;
ALTER TABLE public.commands ADD CONSTRAINT commands_source_check
    CHECK (source IN ('manual', 'schedule', 'timer', 'guard', 'restore', 'auto'));
```

- [ ] **Step 1: 실패 테스트**
  - `test_patch_auto_toggles_only`: `{"auto_vent_enabled": true}` → 200, `auto_mist_enabled is False`, `target_temp_max` 그대로.
  - `test_patch_auto_toggle_rejects_string_and_null`: `"true"` → 422, `null` → 422.
  - `test_get_defaults_false`.
- [ ] **Step 2~4:** 실패 확인 → 구현 → 통과.
- [ ] **Step 5:** 운영 적용 전 `pg_get_constraintdef` 로 현재 CHECK 정의를 확인한다. 적용한 뒤 `BEGIN; INSERT … source='auto' …; ROLLBACK;` 로 통과를 확인한다. 마지막으로 `MIGRATIONS_APPLIED.md` 에 기록한다.
- [ ] **Step 6: 커밋** `feat(auto-env): 설정 토글 2개 + commands.source auto 마이그레이션`

### Task 3: DB 글루 `backend/auto_env_store.py`

**Files:** Create `backend/auto_env_store.py`, `tests/test_auto_env_store.py`

**Context:**
- Depends on: Task 1, Task 2
- Consumes:
  - Task 1 의 API
  - `command_service.insert_pending_command`
  - `scheduling.compute_prev_run`, `scheduling.parse_time_of_day`
  - `alerts._Step`(sb, device_uuid, meta(owner_id), now_mono, now_wall)
- Produces:
  - 스위치 함수:
    - `auto_env_enabled() -> bool`
    - `dry_run() -> bool`(`AUTO_ENV_DRY_RUN`, **기본 True**, "0/false/no/off" 만 거짓)
    - `device_allowed(device_uuid) -> bool`
  - `@dataclass class AutoDeviceState: tracker: AutoEnvTracker; restored: bool; spans: list[dict] | None; spans_expires: float; last_state: dict | None; state_retry_at: float; wet_latch: float | None`
  - `step(c, st: AutoDeviceState, row, th, toggles: tuple[bool, bool], wet_mono: float | None) -> None`
  - `handle_ack(c_like, st: AutoDeviceState, row: dict) -> None`:
    - 자동 명령이면 `on_own_ack`.
    - 아니면 `on_other_ack` + pending 자동 팬 명령 무효화(R7).
    - `mist` ok 면 `on_mist_ok`.
- Must know:
  - 토글 `(False, False)` 이고 소유도 없으면 예약·복원·상태 쓰기를 전부 하지 않는다.
  - 예약 쿼리:
    - `schedules` 에 `.eq("device_id")`, `.eq("enabled", True)`, `.in_("action", ["fan_on","fan_off"])`, `.not_.is_("pair_id","null")`.
    - pair 는 두 행이 다 있어야 유효하다.
    - 명령 INSERT 직전에 캐시를 무시하고 한 번 더 읽는다.
  - 복원 쿼리는 `idx_commands_source` 를 타도록 `.eq("device_id")` 를 먼저 둔다. 팬 쿼리는 source 필터 없이 `.in_("action", ["fan_on","fan_off","fan_toggle"])` + 1시간으로 거른다.
  - pending 무효화는 `.eq("device_id").eq("source","auto").eq("status","pending").in_("action", [...])` 조건부 UPDATE 다.
  - 섀도 모드:
    - INSERT 대신 `logger.info("auto-env intent device=%s action=%s reason=%s", ...)` 를 남긴다.
    - 곧바로 `on_own_ack(action, True)` 를 호출한다.
    - 모의 팬 상태(`fan_on` → 'ON', `fan_off`·1시간 → 'OFF')를 R6 입력 `fan` 대신 쓴다.
    - `auto_env_state` 는 쓰지 않는다. 앱이 가짜 상태를 보면 안 되기 때문이다.
- Acceptance: `uv run pytest tests/test_auto_env_store.py -q` 통과.

- [ ] **Step 1: 실패 테스트** (fake_sb)
  - `test_off_toggles_cost_nothing`: 토글 둘 다 false → `schedules`·`commands`·`devices` 호출 0.
  - `test_insert_auto_command`: 섀도 끔, 토글 on, 습도 85 180초 → `commands` 1행(`source='auto'`, `issued_by=owner`, `reason='auto_vent:humid_high'`). `devices.auto_env_state` 는 ACK 전이라 `active=False`, ACK ok 처리 뒤 `True`.
  - `test_dry_run_logs_only`: 섀도 켬 → `commands` 0행, 로그에 intent 1줄, `auto_env_state` 미기록. 모의 팬으로 1시간 뒤 재발행 intent.
  - `test_insert_failure_retries`: INSERT 예외 → 30초 뒤 재시도.
  - `test_schedule_span_pairs_union`: pair A 10~14시, pair B 12~13시 → 13:30 에 hold True, 15:00 에 False. 한쪽 disabled 인 pair 는 무시.
  - `test_schedule_reread_before_insert`: 캐시엔 예약 없음, INSERT 직전 재조회에서 켜짐 중 → INSERT 0.
  - `test_restore_last_writer`: 40분 전 auto `fan_on` ok + 20분 전 manual `fan_on` ok → 소유 없음. 40분 전 auto `fan_on` ok 단독 → 소유 복원 후 원인 해소 60초 뒤 `fan_off`.
  - `test_restore_mist_from_midnight`: 오늘 자동 분무 3행(0시 이후) + 30분 전 manual mist ok → today 3, 간격 가드 활성.
  - `test_restore_failure_defers`.
  - `test_manual_ack_supersedes_pending_auto`: pending 자동 `fan_off` 1행 + manual `fan_on` ACK → 그 행 `status='expired', result='superseded'`.
  - `test_state_write_only_on_change_and_retry`: 같은 상태 10샘플 → UPDATE 1회. UPDATE 예외 → 60초 뒤 재시도.
  - `test_toggle_off_sends_fan_off`.
- [ ] **Step 2~4:** 실패 확인 → 구현 → 통과.
- [ ] **Step 5: 커밋** `feat(auto-env): 명령 적재·섀도 모드·복원·예약 구간·ACK 처리`

### Task 4: 브리지 연결

**Files:** Modify `backend/alerts.py`, `backend/mqtt/handlers.py`, `tests/test_alerts.py`, `tests/test_mqtt_handlers.py`

**Context:**
- Depends on: Task 3
- Must know:
  - `_DeviceState` 에 `auto: auto_env_store.AutoDeviceState` 필드(default_factory)를 둔다. 재페어링으로 `_DeviceState()` 가 새로 만들어지면 자연히 초기화된다.
  - `_thresholds`:
    - SELECT 에 `auto_vent_enabled, auto_mist_enabled` 를 추가한다.
    - `st.toggles` 에 `(bool, bool)` 로 저장한다(값 없으면 False).
    - 기준이 바뀌면 `st.auto.tracker.reset_timers()` 를 호출한다.
  - `_evaluate_v4` 에서 v4 트래커 루프가 끝난 뒤:
    - `if not st.unlinked:` 블록 안에서 `if auto_env_store.auto_env_enabled() and auto_env_store.device_allowed(device_uuid): step(...)`, 아니면 `st.auto = AutoDeviceState()`(R10 게이트 drop) 를 한다.
    - 블록 전체를 `try/except Exception` + `logger.exception` 으로 감싼다.
  - `wet_mono` 계산:
    - 습기 사이클이 켜져 있고 `st.cycles.tracker` 와 그 `cycle` 이 있으면 `detected_at` 을 쓴다.
    - wall → mono 변환은 `now_mono - (now_wall - detected_at).total_seconds()` 로 한다.
    - 그 값과 `st.auto.wet_latch` 중 큰 값이 `wet_mono` 다.
  - 공개 함수 `alerts.on_command_ack(row: dict) -> None`:
    - `_v4_lock` 안에서 `_states.get(row["device_id"])` 를 찾는다. 없으면 무시한다.
    - 찾으면 `auto_env_store.handle_ack(...)` 를 호출한다.
    - 스위치가 꺼져 있으면 아무것도 안 한다.
  - `handlers._ack_device_command`: UPDATE 결과 행이 있으면 `alerts.on_command_ack(res.data[0])` 를 호출한다. `try/except Exception` 으로 ACK 처리를 보호한다.
  - `_stop_unlinked` 는 손대지 않는다. 해제 기기는 `_evaluate_v4` 앞에서 return 하고, 재페어링 때 `_DeviceState` 가 교체된다.
- Acceptance: `uv run pytest -q` 전체 통과(기존 테스트 불변). 스위치가 꺼져 있으면 `commands` INSERT 0, `device_settings` 쿼리 수 불변.

- [ ] **Step 1: 실패 테스트**
  - `test_auto_env_off_by_default`: step 호출 0, 쿼리 수 기존과 같음.
  - `test_auto_env_error_does_not_block_v4`: step 예외 → v4 알림 행 정상.
  - `test_switch_off_drops_auto_state`.
  - `test_ack_calls_auto_hook`(test_mqtt_handlers): 훅 예외여도 ACK UPDATE 는 유지.
- [ ] **Step 2~4:** 실패 확인 → 구현 → 통과.
- [ ] **Step 5: 커밋** `feat(auto-env): 브리지 판정·ACK 연결 (AUTO_ENV_ENABLED 꺼짐·섀도 기본)`

### Task 5: 문서·앱 회신·앱 팀 전달 보고서

**Files:** Create `docs/APP_AUTO_ENV_2026-11-05.md` · Modify `docs/ENV.md`, `docs/API.md`(settings 필드, 버전 올림), `docs/DATABASE.md`

**Context:**
- Depends on: Task 1~4, `/code-review` 반영
- Must know: 앱 회신은 요청서 §4 C1~C5 와 §6 질문 6개에 1:1 로 답한다.
  - §6-1: 서버 판정 + 섀도 모드 단계.
  - §6-2: 확정 숫자 표(우리 안전값 4개, 종료선 공식 포함).
  - §6-3: 환기팬만.
  - §6-4: 고정 3초.
  - §6-5: 구간 예약 켜짐 구간만이다. 분무는 구간 예약이 없어서 "분무·예약 실행 중" 상태가 서버에선 생기지 않는다. 단발 예약·건너뛴 회차는 고려하지 않는다. 예약이 끝난 뒤 자동이 다시 켤 수 있다.
  - §6-6: 자동 작동 푸시 없음. 물통 비움 의심은 이번 범위 밖이다.
  - 그 밖에 적을 것:
    - 앱 화면 문구는 실제 종료선, 추운 날 멈춤, 하루 12시간 상한을 설명한다.
    - 토글 반영은 최대 60초 늦을 수 있다.
    - 알려진 한계 두 가지: 자동 타이머 중 사용자 timer 칩은 `busy` 가 날 수 있다(펌웨어 확인 결과로 갱신). 이미 나간 자동 명령과 사용자 명령이 수백 ms 안에 겹치는 경합이 남는다.
- Acceptance: 문서 수치를 Task 1 상수와 grep 으로 대조해 일치.
- [ ] **Step 1:** 작성 → **Step 2:** 커밋 `docs(auto-env): 앱 회신·ENV·API·DB`.
- [ ] **Step 3: 앱 팀 전달 보고서** (사용자 요청 2026-11-05) — PR 까지 끝난 뒤 작성한다.
  - `docs/APP_AUTO_ENV_2026-11-05.md` 맨 위에 "배포 상태"를 넣는다: PR 번호, 머지·배포 여부, 스위치·섀도 상태, 앱이 토글을 붙여도 되는 시점.
  - Slack 붙여넣기용 짧은 요약(§6 답 + 앱 할 일)을 따로 만든다.
  - 발송은 사용자 승인 후.

## 켜는 순서 (머지·배포·플래그는 이관훈님)

0. ✅ 착수 전: 펌웨어 타이머 동작 3가지 확인 완료(2026-11-05, nano·supermini 소스 — [docs/TEMP_OFFSET_BASE_DECISION_REPLY_2026-11-05.md](../docs/TEMP_OFFSET_BASE_DECISION_REPLY_2026-11-05.md) §B): 타이머 중 `fan_on{duration_ms}` → `busy`(연장 아님) / 일반 `fan_on` → 자동 OFF 예약 취소, 계속 ON / `fan_off` → fan·fan2 모두 타이머 취소 후 OFF. R6·R7 가정과 일치.
1. ✅ 마이그레이션 운영 적용(2026-11-05, `MIGRATIONS_APPLIED.md`).
2. ✅ PR #48 머지·배포(2026-11-05 11:13 UTC, 리뷰 반영 0b3c812 포함). `AUTO_ENV_ENABLED` 꺼짐이라 변화 없음 — 배포 후 `source='auto'` 0건·에러 0건 확인.
   - **앱팀 확정(2026-11-05)**: 테스트 기기 = **사육장 1 `terra-d9feaaa2`(tester01 계정)** → `AUTO_ENV_DEVICES` 에 이 기기 UUID. 앱 토글은 "전체 켬" 알림 전까지 숨김. `source='auto'` 기록 표시 + 자동 팬을 사용자 타이머로 세지 않는 수정은 APK 0.174.2 에 포함. "1대 실제 명령" 단계 진입 시 앱팀에 알리면 실기기 확인.
   - 앱 §6 회신 반영: 습도 1시간 문구 변경 없음 / 예약 실행 중 판정은 앱이 예약 목록으로(미구현, 지금은 토글 자유) / 제어 기록 합치기 기준 "끝 + 8분".
3. `ALERTS_V4_ENABLED` 켜진 상태에서 `AUTO_ENV_ENABLED=true` + `AUTO_ENV_DRY_RUN` 기본(켜짐) + `AUTO_ENV_DEVICES=<토글 켠 테스트 기기>` 로 **섀도 2~3일**을 돌린다.
   - 게이트 1: 기기·일당 자동 환기 시작 ≤ 6(중앙값 ≤ 3).
   - 게이트 2: 환기 1회 길이 중앙값 ≥ 10분.
   - 게이트 3: 자동 분무 ≤ 4.
   - 게이트 4: 습도 원인 시작은 모두 습도 > MAX 1시간 연속 뒤(로그 시각으로 확인).
   - 하나라도 넘으면 사용자에게 보고한다.
4. 펌웨어 확인 결과가 R6·R7 가정과 맞으면 `AUTO_ENV_DRY_RUN=false` 로 바꾼다(테스트 기기 1대). (r2 의 "습기 사이클 먼저 켜기" 조건은 r3 에서 분무 뒤 보호가 없어져 불필요.) 실기기에서 켜짐·꺼짐·수동 끔·분무를 확인한다.
5. 앱 토글 출시 + `AUTO_ENV_DEVICES` 비움(전체).

롤백: `AUTO_ENV_ENABLED=false` 로 재시작한다. 켜져 있던 자동 팬은 펌웨어가 1시간 안에 끈다.

## 가정

| 가정 | 상태 |
|---|---|
| `fan_on` + `duration_ms` 는 펌웨어가 2시간 상한 안에서 스스로 끈다 | 확인 (§3.2) |
| 타이머 중 타이머 명령 재발행은 `busy` | 확인 (§3.3(2)) — 그래서 갱신 없음(R6) |
| 타이머 중 `fan_off` 는 자동 OFF 도 취소 | 확인 (§3.2, fan2 기준 같은 로직) |
| 타이머 중 **일반 `fan_on`** 이 자동 OFF 를 지우는지 | **미확인** — 안 지운다고 보고 설계. 이 경우 사용자가 자동 환기 중에 "켜기"를 눌러도 자동 타이머 끝에 꺼질 수 있다. 켜는 순서 0 에서 확인, 회신에 한계로 적음 |
| 텔레메트리 `fan` 이 실제 팬 상태를 3초 안에 반영 | 확인 (§3.2) |
| 분무 ACK `ok` = 펌프가 돌았음(물이 나왔는지는 모름) | 확인 — 분무 간격(R9)에만 쓰고, 환기 판정은 습도 1시간 연속으로만(r3) |
| 앱이 `auto_env_state` 로 C3 를 그린다 | 미확인(회신으로 제안) |

## 실패 시나리오

| 시나리오 | 대응 |
|---|---|
| 판정 버그로 팬이 계속 켜짐 | 매 명령 1시간 자동 OFF, 하루 12시간 상한, 스위치·허용목록으로 즉시 차단 |
| 판정 버그로 분무 폭주 | 시도 순간 예산 소진 + 하루 4회·2시간 간격(모든 source) + 회복 전 재무장 없음 |
| 센서가 99.9% 로 고정 | 하루 12시간 상한(R5), 섀도 게이트 |
| 자동 `fan_off` 가 사용자 `fan_on` 직후 도착 | 소유는 자동 ACK ok 에서만 확정되고 사용자 ACK 에서 해제된다. pending 자동 팬 명령도 무효화하고, 디스패처가 발행 직전 재확인한다. 이미 나간 명령과의 수백 ms 경합은 남는다 |
| 재시작 뒤 남의 팬을 자동 소유로 오인 | 마지막 작성자 판정(R11) |
| 브리지 DB 지연으로 v4 판정이 느려짐 | 토글 꺼진 기기는 추가 쿼리 0, DB 호출은 전이·복원 때만 |
| 겨울 환기로 저온 | 온도 ≤ MIN 이면 습도 환기 금지·중단(R2·R5) |

## 검증 방법

- 단위: `uv run pytest tests/test_auto_env.py tests/test_auto_env_store.py tests/test_settings_api.py -q`, 전체 `uv run pytest -q`.
- 섀도: 켜는 순서 3 의 게이트(운영 로그 `auto-env intent` 집계, 스크립트는 `reports/` 에 미커밋).
- 실기기: 켜는 순서 4. 사용자 또는 이관훈님만 확인할 수 있다.

## 사용자가 정할 것

없음 — 2026-11-05 결정 반영·승인 완료(위 Global Constraints). 앱 회신 문서는 사용자 요청으로 구현 전에 먼저 쓰고(Task 5 Step 1 선행), PR 뒤 "배포 상태"를 갱신한다(Task 5 Step 3).
