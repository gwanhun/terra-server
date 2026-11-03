# Stage O — 습기 사이클 (습기 이벤트·건기·환기 지연·분무 공백, 기획 v4 1-2·1-3·1-6) Pre-Plan

> **구현 방식 (CAOF):** Critical 트랙. 상태: ⏸️ **기획 확정 대기** (2026-11-02).
> 이 문서는 구조·파일·인터페이스·테스트 이름까지 확정하고, 기획 값은 결정 번호(O-a~O-g)로 참조한다. 결정이 오면 r2 에서 값을 채워 재승인받은 뒤 착수한다.
> **선행: Stage N 머지** (env_rules·alerts v4 경로·push 이벤트를 재사용).

**Goal:** 분무 등으로 습도가 오른 "습기 이벤트"를 기록하고, 이벤트마다 건기 목표(MAX − 12%p) 도달 여부·소요 시간을 남기며, 6시간 안에 못 마르면 환기 지연, 15시간 동안 새 습기 이벤트가 없으면 분무 공백 알림을 낸다.

**Architecture:** 습기 사이클은 알림이 아니라 "구간 기록"이라 `alerts` 와 분리한 `humid_cycles` 테이블(이벤트 1개 = 1행)에 둔다. 알림이 되는 두 가지(환기 지연·분무 공백)만 `alerts` 의 v4 kind 로 기록하고 Stage N 푸시 경로를 탄다. 판정은 순수 상태 기계 `backend/humid_cycle.py`, 연결은 `backend/alerts.py` v4 경로에 추가.

**Tech Stack:** Python 3.12, uv, supabase-py, pytest, Supabase SQL 마이그레이션

**Spec:** 노션 v4(2026-11-02 수정본) 0-2 용어, 1-2 습도 기준, 1-3 환기 지연·분무 공백, 1-4 습도 푸시, 1-5 문구, 1-6 기록 기준. 실측 근거: 베타 10명 기기 분무 116회 분석(아래).

## 착수 조건 — 기획 결정 대기

| # | 결정할 것 | 현재 문서 | 우리 실측·관찰 | 계획에 미치는 영향 |
|---|---|---|---|---|
| O-a | 습기 이벤트 발생 조건: N분, "60%"의 의미, 분무 전부터 60% 이상인 경우, "급상승" 기준 | 1-2 `{N분} 이내 습도가 60% 이상 급상승` | 60% 도달로 해석 시 기기 분무 82% 감지(분무 전 ≥60% 인 13% 누락), +20%p 로 해석 시 55%. 상승은 2~3분 안에 끝남 | `HumidCycleTracker` 의 감지 함수 |
| O-b | 기기 분무 명령(`commands` mist ok)을 습기 이벤트로 쓸지 | 언급 없음(v1 `{control_time}` 만) | 기기 분무는 서버가 시각을 정확히 앎 | 감지 입력에 명령 이벤트 추가 여부 |
| O-c | 습기 이벤트 종료 조건 = MAX 측정 창 | 미정 사항에서 삭제됨 | 최고점 도달 중앙값 2.4분, p90 12분 | MAX 확정 시점 → 건기 목표 확정 시점 |
| O-d | 분무 직후 90~100% 제외 기준 | 1-2 "기준 정해야 됨" | 기기 분무 116회 중 90% 이상 0회(최고 85%) | MAX 계산 필터 유무 |
| O-e | 15시간 분무 공백 알림의 이름·푸시 여부 | 1-3 `건조 주의`(MIN 미만과 같은 이름), 1-4 습도 표에 없음 | — | kind·푸시 여부 |
| O-f | "분석 기준 시점에 미도달 기록"의 시점 | 0-2·1-6 `분석 기준 시점` | 08:00 마감 시 07:00 분무는 1시간 만에 미도달 | 일일 분석과의 경계 처리 |
| O-g | 연속 분무(짧은 간격) 병합 여부와 간격 | 없음 | 2분 이내 연속 명령 25건/303 | 병합 창 |
| O-h | 센서 연결이 끊긴 시간을 6시간 환기 지연 판정에서도 빼는지 | 1-3 은 15시간 분무 공백에만 "습기 미감지 시간에 포함하지 않는다" | — | 환기 지연 타이머 |

## 결정과 무관하게 확정하는 것

| # | 결정 | 근거 |
|---|---|---|
| O1 | `humid_cycles` 테이블: 습기 이벤트 1개 = 1행. `source`(`device_mist`/`humidity_rise`), `started_at`, `max_humid`, `dry_target`, `dry_reached_at`, `status`(`open`/`reached_in_time`/`reached_late`/`not_reached`), `vent_delay_alert_id` | 1-6 "습기 이벤트별 MAX값과 건기 목표를 별도로 저장", 주간 `{dry_time}` 계산 입력 |
| O2 | 건기 목표 = 그 이벤트 MAX − 12%p, 도달 = 습도 ≤ 건기 목표인 첫 샘플(2분 유지 같은 확인 없음 — 1-6 "최초 도달 시각") | 0-2, 1-6 |
| O3 | 환기 지연 = 이벤트 시작 후 6시간 시점에 미도달 → `alerts` `env_vent_delay`(severity `warning`) 생성·푸시 `[환기 지연] 분무 후 건기 미도달`. 이후 도달하면 종료(`resolved_at` = 도달 시각), `status=reached_late` | 1-3, 1-4 |
| O4 | 새 습기 이벤트가 열리면 직전 이벤트가 미도달이면 `not_reached` 로 닫고, 진행 중 `env_vent_delay` 도 종료 | 1-6 |
| O5 | 분무 공백 알림은 마지막 습기 이벤트 시작 + 15시간, 센서 연결 끊긴 시간은 제외(오프라인 구간만큼 연장). 신규 기기(습기 이벤트 0회)는 발생 안 함 | 1-3 |
| O6 | 사이클 판정 상태(마지막 이벤트 시각, 진행 중 이벤트)는 `humid_cycles` 에서 복원 — Stage N 과 같은 재시작 정책 | Stage N D-복원 |

## Tasks (값은 결정 번호 참조 — r2 에서 숫자로 교체)

### Task 1: 순수 상태 기계 `backend/humid_cycle.py`
- Produces: `class HumidCycleTracker`, `.on_sample(now_mono, humid: float | None) -> list[CycleAction]`, `.on_mist_command(now_mono)`(O-b 채택 시), `.on_offline(now_mono, duration_sec)`, `.restore(...)`; `CycleAction(op: Literal["cycle_open","cycle_max","dry_reached","cycle_close","vent_delay","mist_gap"], ...)`
- 테스트 이름(값은 결정 후): `test_rise_detects_wet_event`(O-a), `test_already_high_baseline`(O-a), `test_device_mist_opens_event`(O-b), `test_max_window_closes`(O-c), `test_spike_excluded`(O-d), `test_dry_reached_in_time`, `test_dry_reached_late_closes_vent_delay`, `test_new_event_marks_not_reached`, `test_vent_delay_at_6h`, `test_mist_gap_15h_excludes_offline`(O5), `test_new_device_no_mist_gap`, `test_consecutive_mist_merged`(O-g)

### Task 2: 마이그레이션 `humid_cycles` (+ RLS 본인 SELECT, Realtime)
- 컬럼은 O1. 선언 전문은 r2 에서(O-a·O-b 결정에 따라 `source` 값 확정).

### Task 3: `backend/alerts.py` v4 경로 연결
- 습도 샘플마다 `HumidCycleTracker` 호출, `humid_cycles` INSERT/UPDATE, `env_vent_delay`·`env_mist_gap`(O-e) 을 Stage N 과 같은 open/close/push 경로로.
- O-b 채택 시 `handle_ack` 의 mist ok 지점에서 트래커에 알림(지연 임포트, 실패 무시).

### Task 4: 문서 — 앱 핸드오프(`humid_cycles` 구독, 신규 kind), 일일 분석 연동(Stage P P9 해제)

## 이 Stage 가 풀어 주는 것

- Stage P P9: 습도 이벤트 없는 날 카드(`건습 사이클이 건강하게 유지됐어요` / `습기가 감지되지 않았어요`).
- Stage Q: HUM_DELAY, HUM_NORMAL, `{dry_time}`.

## r2 에서 함께 정할 구조 항목 (교차 검수 반영 — `reports/stage-opq-v4-review-final.md`)

- **활성 플래그 소유:** `HUMID_CYCLES_ENABLED`(기본 false)를 이 Stage 가 정의한다(Produces). Stage P(`build_humid_card(cycles=…)`)·Q(`build_weekly(cycles=…)`)는 이 플래그로 `humid_cycles` 를 넘긴다. P9 해제는 문서가 아니라 P·Q 러너의 조회 코드·테스트 변경을 포함한다.
- **kind 범위:** 이 Stage 의 `env_vent_delay`·`env_mist_gap` 은 Stage N 복원·`close_v4_alerts.py` 대상이 아니다(N 은 4개 kind 를 정확히 열거). 환기 지연 행 `context.cycle_started_at` 에 분무(습기 이벤트) 시작 시각을 기록 — Stage P 문구 `{start}에 분무 후 …` 가 6시간 늦은 시각을 쓰지 않게.
- **오프라인 처리:** 오프라인 판정 출처(`offline_monitor` 의 offline 판정 또는 telemetry 간격 > 60초)와 `on_offline` 호출 경로, 누적 제외 시간과 진행 중 오프라인 시작 시각(wall)을 `humid_cycles` 또는 기기 상태에 저장해 재시작 복원. 6시간 환기 지연에도 같은 제외를 적용할지(O-h 로 기획 확인).
- **MAX 확정 전 건기 도달:** MAX 측정 창(O-c)이 끝나기 전에는 건기 도달 판정을 하지 않는다.
- **분무 공백 알림 수명:** 새 습기 이벤트가 열리면 `env_mist_gap` 종료, 같은 공백 중 재발생 없음, 재시작 시 마지막 습기 이벤트 시각에서 복원.
- **명령 멱등:** O-b 채택 시 `on_mist_command(command_id, …)` — 같은 command_id 는 한 번만, 명령으로 열린 이벤트 직후 같은 상승을 습도 감지가 중복으로 열지 않게(O-g 병합 창 안이면 무시).
- **부분 실패:** `humid_cycles` 쓰기·alerts 기록·푸시 적재의 실패 처리는 Stage N Task 2 정책(open 실패 시 롤백, close 재시도 목록, 적재 10분 재시도)을 따른다. 실패 주입 테스트 포함.
- **not_reached 의미(O-f):** 마감 시점 미도달이 최종인지 중간 판정인지. 중간이면 이후 도달 시 `reached_late` 로 바꾸고, Stage P 2차 생성(09:05)이 정정을 흡수하는 범위를 명시.
