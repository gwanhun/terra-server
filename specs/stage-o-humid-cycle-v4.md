# Stage O — 습기 사이클 (습기 이벤트·건기·환기 지연·분무 공백, 기획 v4 1-2·1-3·1-6) Implementation Plan r3

> **구현 방식 (CAOF):** Critical 트랙. 이 계획을 task 단위로 구현한다(worktree `feat/humid-cycle-v4`, TDD). Steps use checkbox (`- [ ]`) syntax for tracking.
> 상태: 🚧 **구현 완료(2026-11-03, 브랜치 `feat/humid-cycle-v4`, 스위치 꺼짐, 마이그레이션 운영 적용 완료)** — r3 승인 2026-11-03. 구현 메모: op 쓰기는 스냅숏 되돌림 대신 기기별 대기열(순서대로, 실패한 op 에서 멈춤)로 — 같은 보장(open 먼저·종료 나중·재시도)을 더 단순하게.
> 이력: r3(2026-11-03) — 교차 검수 반영. r1(Pre-Plan, 11-02) → r2(결정 확정, 11-03) → r3(검수 `reports/stage-o-v4-r2-review-final.md` 반영). 기획자 답이 없는 ⑦⑧㉕는 기획 원문 + 베타 실측으로 우리가 정했다(사용자 2026-11-03 "기획자 티키타카는 끝난 것으로 간주").
> **진행 순서:** Task 1(순수 상태 기계) → **Task 2 리플레이 게이트**(베타 7일) → 통과하면 Task 3~6. 게이트에서 막히면 마이그레이션 전에 사용자에게 보고.

**Goal:** 습도가 급상승한 구간을 "습기 이벤트"로 기록하고(`humid_cycles` 1행), 이벤트마다 MAX·건기 목표(MAX − 12%p)·건기 도달 시각을 남기며, 6시간 안에 못 마르면 환기 지연, 건기에 닿은 뒤 15시간 동안 새 습기 이벤트가 없으면 분무 공백 알림을 낸다. 일일·주간 습도 카드의 습기 사이클 문구(P9·Q10·㉕)를 켠다.

**Architecture:** 판정은 순수 상태 기계 `backend/humid_cycle.py`(샘플 시각 입력, DB 모름, 스위치 함수도 여기). DB·푸시 연결은 `backend/humid_cycle_store.py`, 호출은 Stage N v4 경로(`alerts._evaluate_v4`)에서 습도 샘플마다 한 번 — Stage N 설정·복원과 독립. 습기 이벤트는 구간 기록이라 `alerts` 와 분리한 `humid_cycles` 테이블에 둔다. 알림이 되는 두 가지(환기 지연 `env_vent_delay`, 분무 공백 `env_mist_gap`)만 `alerts` 에 v4 행으로 남기고 Stage N 과 같은 `env.alert.triggered` 푸시를 탄다. 스위치 `HUMID_CYCLES_ENABLED` + 기준 시각 `HUMID_CYCLES_SINCE`(기본 꺼짐, `ALERTS_V4_ENABLED` 경로에서만).

**Tech Stack:** Python 3.12, uv, supabase-py, pytest, Supabase SQL 마이그레이션

**Spec:** 노션 v4(2026-11-03 기준) 0-2 용어, 1-2 습도 기준, 1-3 실시간 판정, 1-4 습도 푸시, 1-5 문구, 1-6 기록 기준, 2-3 일일 습도, 3-3 주간 습도. 노션 댓글 ⑤-1·⑥·⑲(기획자 답 11-03). 실측: 베타 기기 분무 116회(`mist_rise`, 11-02), 베타 10대 7일 원본 습도 50 기기·일(`rise_fp`, 11-03). 앱 수신 준비: 앱 핸드오프 §8(앱 0.168.0).

## 결정

| # | 결정 | 값 | 근거 |
|---|---|---|---|
| O-a | 습기 이벤트 감지 | 습도 = 유효 샘플(a_ok, 숫자)의 **30초 이동 중앙값**. 지난 **3분** 최저값 대비 상승 ≥ **10%p**(최저값 < 60%) 또는 ≥ **5%p**(최저값 ≥ 60%), 그리고 현재값 ≥ **60%**. 이벤트 시작 = 그 최저값 시각(같은 값이면 가장 늦은 샘플). 공백 뒤엔 중앙값 표본 ≥ 5개·창 이력 ≥ 60초가 쌓이기 전엔 감지 안 함 | 기획자 답(⑤-1) + 우리 답(60% 이상 시작 +5%p). 실측 분무 재현 +10%p 91/107 → 100/107, 새 명령 없는 감지 0.08/기기·일 |
| O-b | 기기 분무 명령 | 감지 입력으로 **쓰지 않는다** | 사용자 결정(11-02): 펌프 막힘·물통 빔 |
| O-c | MAX | 상승 **감지 시각 + 3분**까지의 최고 습도(30초 중앙값). 그 창이 오프라인이면 관측한 최고값으로 확정 | 기획자에게 알린 "시작 후 3분"의 시작을 감지 시각으로 — 최저점 기준이면 감지 몇 초 뒤 창이 닫혀 MAX 가 작게 잡힘(검수) |
| O-d | 튀는 값 | 제외 규칙 없음. **MAX 상한 85%**(측정 최고값은 `max_raw`) | 기획자 답(⑥) |
| O-e ⑦ | 15시간 분무 공백 | kind `env_mist_gap`, severity `warning`(level `caution`), `[건조 주의] 분무 필요`. 푸시 **최초 1회**. 조건: 최근 습기 이벤트가 **건기에 도달**했고, 그 시작부터 유효 15시간 동안 새 습기 이벤트 없음 | 우리 결정. 문구는 1-5 원문, 푸시 기준은 위임 영역. 아직 안 마른 사육장에 "분무 필요"를 보내지 않음(검수) |
| O-f ⑲ | 하루 경계 | 사이클 일일 소속 = report_date **05:00 ~ 다음 날 05:00(KST)** 시작. 건기 도달 수 = 그중 카드 구간 끝(다음 날 08:00) 이전 도달. 주간도 05:00 경계·같은 컷오프 | 기획자 답(⑲) |
| O-g | 연속 상승 병합 | 이벤트 시작 후 **20분 안**, 건기 도달 전, 앞 상승의 MAX 창이 끝난 뒤 시작한 새 급상승은 **같은 이벤트** — MAX 창을 새 감지 + 3분으로 다시 열고 건기 목표를 비웠다가 재확정 | 11-02 "5분"에서 확대(**사용자 확인 필요**). 실측 연속 감지 간격 15분 안 45건 → 15~30분 2건. 기획자 답 "이미 습한데 한 번 더 뿌리면 하나의 습기 이벤트" |
| O-h ⑧ | 6시간 환기 지연과 오프라인 | 센서 공백을 6시간에서도 뺀다. 환기 지연 푸시 **최초 1회**. 유효 15시간이 지나도 안 마르면 환기 지연 행을 닫는다(`closed_reason="stale"`, 카드에 같은 줄이 매일 남지 않게) | 우리 결정(1-3 의 15시간 규칙과 같게) |
| ㉕ | 습기 이벤트 없는 주 | 주간 습도 카드 `HUM_NO_MIST`(caution) `습기가 감지되지 않았어요` / `분무를 잊지 않으셨는지 확인해 주세요` — 습도 이벤트 0 + 사이클 0 일 때만 | 우리 결정. 일일 2-3 같은 상황 문구를 그대로 |

**r1 확정 사항(유지·보강)**

| # | 결정 |
|---|---|
| O1 | `humid_cycles` 1행 = 습기 이벤트 1개(Task 3 전문) |
| O2 | 건기 목표 = MAX − 12%p, MAX 확정 때 정함(병합이면 재확정). 건기 도달 = 확정된 목표 이하 첫 샘플(30초 중앙값), 이후 다시 올라도 기록 유지. 같은 샘플에서 6시간 경계와 겹치면 도달 판정이 먼저 |
| O3 | 환기 지연 = 유효 경과 6시간에 미도달 → `env_vent_delay`(warning/caution) `[환기 지연] 분무 후 건기 미도달`. 이후 도달 → `resolved_at` = 도달 시각, 사이클 `reached_late` |
| O4 | 미도달 상태에서 새 습기 이벤트 → 직전 사이클 `not_reached`, 열린 두 알림 종료(`resolved_at = max(새 이벤트 시작, 그 알림 triggered_at)`) |
| O5 | 사이클 0개(신규·스위치 켠 직후·해제 뒤) 기기는 분무 공백 없음 |
| O6 | 복원: Task 4 규칙 |

**권장 시간 내 도달:** 유효 경과(도달 − 시작 − 오프라인 초) ≤ 6시간 → `reached_in_time`, 아니면 `reached_late`. 주간 `{dry_time}` 은 벽시계(도달 − 시작).

## 유저 체험 시뮬레이션

1. 저녁 20:00 분무 → 습도 48% → 70%(2분). 20:01 감지, 습기 이벤트 시작 20:00. 20:04 MAX 70% 확정, 건기 목표 58%.
2. 20:12 한 번 더 분무 → 74%. 같은 이벤트(20분 안)라 MAX 창이 다시 열려 20:16 MAX 74%, 건기 목표 62%로 재확정.
3. 01:10 61.8% → 건기 도달(5시간 10분, 권장 시간 내). 푸시 없음.
4. 다음 날 08시대 일일 카드(이벤트 없음): `건습 사이클이 건강하게 유지됐어요` / `습기 1번, 건기 1번이 기록됐어요`.
5. (다른 날) 02:00 까지 62% 위 → 6시간째 `[환기 지연] 분무 후 건기 미도달` 푸시 1회. 03:30 도달하면 종료(푸시 없음), 일일 카드 `건습 사이클이 건강하지 않아요` / `20:00에 분무 후 사육장이 충분히 마르지 않음`.
6. 건기에 닿은 뒤 다음 날 11:00(20:00 + 15시간)까지 새 급상승이 없으면 `[건조 주의] 분무 필요` 푸시 1회. 분무해서 급상승이 잡히면 끝.
7. 밤새 기기가 꺼져 있던 3시간은 6·15시간 타이머에서 빠진다.

## Global Constraints

- 스위치: `HUMID_CYCLES_ENABLED`(true/1/yes/on, 기본 꺼짐) + `HUMID_CYCLES_SINCE`(켠 시각 ISO8601, 없으면 스위치가 켜져 있어도 판정·카드 안 함). 둘 다 `backend/humid_cycle.py` 의 `humid_cycles_enabled()`·`humid_cycles_since()` 로 읽고, 브리지 판정·분석 러너가 같은 함수를 쓴다. `ALERTS_V4_ENABLED` 가 꺼져 있으면 판정 안 돈다. 푸시는 `PUSH_ENV_ALERTS_ENABLED` 뒤.
- kind: `env_vent_delay`, `env_mist_gap`. `context.rule = "v4"`, `context.level = "caution"`, `message` = `[환기 지연] 분무 후 건기 미도달` / `[건조 주의] 분무 필요`.
- 샘플 시각: `row.ts`(기기 시계)가 서버 wall 과 ±10분 안이면 `row.ts`, 아니면 wall. 직전 샘플보다 이르면(역순) 그 샘플은 사이클 판정에서 건너뜀. 공백 판정 60초.
- 상수: `SMOOTH_SEC=30`, `SMOOTH_MIN_SAMPLES=5`, `RISE_WINDOW_SEC=180`, `RISE_MIN_HISTORY_SEC=60`, `RISE_PP=10.0`, `RISE_PP_HIGH_START=5.0`, `HIGH_START_PCT=60.0`, `WET_MIN_PCT=60.0`, `MAX_WINDOW_SEC=180`, `MAX_CAP_PCT=85.0`, `DRY_DROP_PP=12.0`, `MERGE_SEC=1200`, `VENT_DELAY_SEC=21600`, `MIST_GAP_SEC=54000`, `VENT_STALE_SEC=54000`, `GAP_SEC=60`, `CLOCK_SKEW_SEC=600`, `RETRY_SEC=600`.
- service_role 쿼리는 `device_id` 로 거른다.
- 쓰기는 모든 컬럼을 코드가 명시(`updated_at` 포함 — 트리거 없음, FakeSB 기본값 없음).
- 건드리지 않는 것: `env_rules.py`, Stage N 4개 kind 복원·종료, legacy 경로, 이벤트 요약 문구, 푸시 payload 형식(kind 값만 추가).

## Review Focus

1. **브리지 재시작·스위치 재활성·같은 행 재페어링** — 오래된 사이클로 환기 지연·분무 공백이 한꺼번에 울리지 않고, 열린 알림이 두 번 열리거나 푸시가 두 번 나가지 않아야 한다. → Task 4 `test_restore_*`, `test_reenable_no_storm`, `test_repair_starts_fresh`.
2. **공백·센서 오류·재연결 몰림** — 공백 동안 아무것도 열리지 않고, 공백 앞뒤 값 차이나 몰려 들어온 샘플로 가짜 급상승이 잡히지 않아야 한다. → Task 1 `test_gap_resets_rise_window`, `test_reconnect_spike_needs_history`, Task 4 `test_burst_uses_row_ts`.
3. **한 번 상승 뒤 고원** — 병합이 반복되지 않아야 한다. → Task 1 `test_single_rise_plateau_no_merge`.
4. **시작 습도가 높은 사육장** — 건기 목표가 시작 습도보다 낮아 매번 환기 지연이 나는지 → Task 2 리플레이 게이트(시작 습도 구간별).
5. **스위치 켠 날·습도 무효인 날** — "습기가 감지되지 않았어요" 오카드가 나오지 않아야 한다. → Task 5 `test_since_gate_*`, `test_invalid_day_no_cycle_card`.

## 가정

| 가정 | 상태 |
|---|---|
| 규칙이 운영에서도 분무를 90% 이상 잡는다 | 확인(감지 규칙만 리플레이 100/107) — Task 2 에서 전체 상태 기계로 재확인 |
| 명령 없는 급상승 대부분이 손분무다 | **미확인**(정답 라벨 없음). 명령 0 기기의 아침 반복 상승으로 추정 |
| `telemetry.ts` 는 기기 시계(`handlers._normalize_ts`, 형식이 안 맞으면 서버 now), 현재 샘플은 평가 전에 INSERT 됨(`handlers.py:975` → `:1038`) | 확인(검수 지적 후 코드 확인) — 재시작 공백은 `ts < row.ts` 인 직전 유효 행으로 |
| alerts.kind 에 CHECK 제약 없음 | 확인 |
| 앱이 두 kind 를 받을 준비가 됨, 칩은 alerts 로 그림(`humid_cycles` 구독 안 함) | 확인(앱 §8) → Realtime publication 안 함 |

## 실패 시나리오와 대응

| 시나리오 | 대응 |
|---|---|
| open INSERT 실패·응답 유실 | 전이 전체(직전 사이클 종료 포함)를 쓰지 않고 보류, 같은 `started_at` 으로 10분 동안 샘플마다 upsert 재시도(`UNIQUE(device_id, started_at)` → 중복 없음). 성공한 뒤에 종료들을 쓴다 |
| UPDATE 실패 | 기기별 재시도 목록(테이블·id 키), 다음 샘플에서 다시 |
| 알림 INSERT 실패·응답 유실 | 다음 샘플에서 재시도, INSERT 전 같은 kind 활성 행이 있으면 그 행을 쓴다 |
| 푸시 적재 실패 | 10분 동안 재시도(직전 `_confirm_linked`), 성공 시 `context.pushed_at`. 재시작 뒤엔 `pushed_at` 없는 10분 안 행을 재시도 |
| 스위치 껐다 켬 | 끌 때 `scripts/close_v4_alerts.py --cycles --apply`(열린 두 kind 종료 + 최근 사이클 `ended_by='disabled'`), 켤 때 새 `HUMID_CYCLES_SINCE` |
| 리플레이 게이트 미통과 | 마이그레이션 전 사용자에게 보고, 값·기획 문제 재검토 |

## Tasks

### Task 1: 순수 상태 기계 `backend/humid_cycle.py`

**Files:** Create `backend/humid_cycle.py` · Test `tests/test_humid_cycle.py`

**Context:**
- Depends on: 없음
- Produces:
  - `def humid_cycles_enabled() -> bool`, `def humid_cycles_since() -> datetime | None`
  - `@dataclass CycleState`: `cycle_id: str | None`, `started_at: datetime`, `start_humid: float`, `detected_at: datetime`(마지막 병합 상승의 감지 시각), `peak: float`, `max_until: datetime`, `dry_target: float | None`, `dry_reached_at: datetime | None`, `status: Literal["open","reached_in_time","reached_late","not_reached"]`, `offline_sec: float`, `merged_count: int`, `vent_delay_alert_id: str | None`, `vent_delay_fired: bool`, `vent_delay_closed: bool`, `mist_gap_alert_id: str | None`, `mist_gap_fired: bool`
  - `@dataclass(frozen=True) CycleOp`: `op: Literal["open","merge","max_fixed","dry_reached","not_reached","vent_delay","vent_delay_close","mist_gap","mist_gap_close","offline"]`, `at: datetime`, `cycle: CycleState`, `value: float | None`, `reason: str | None`(`vent_delay_close` 의 `dry`/`next_event`/`stale`)
  - `class HumidCycleTracker`: `__init__(cycle: CycleState | None = None, last_sample_at: datetime | None = None)`, `.step(now: datetime, humid: float | None) -> list[CycleOp]`, `.snapshot() -> object`, `.restore_snapshot(s) -> None`(store 가 전이 쓰기 실패 시 되돌림), `.cycle`
  - `def max_humid(peak: float) -> float`(min(peak, 85), 소수 1자리), `def effective_elapsed(c: CycleState, now: datetime) -> float`
- Must know:
  - `humid is None` 은 무시(마지막 유효 샘플 시각 유지). 유효 샘플 간격 > 60초면 간격 전체를 **최근 사이클(열림·닫힘 무관)** `offline_sec` 에 더해 `offline` op, 중앙값 버퍼·3분 창 비움.
  - 감지 가능 조건: 중앙값 버퍼 표본 ≥ 5, 3분 창 이력 ≥ 60초.
  - **새 상승 판정:** 급상승 조건을 만족하고 그 최저점 시각 ≥ 현재 사이클 `max_until`(사이클 없으면 무조건). 아니면 같은 상승으로 보고 무시.
  - 새 상승이면: 현재 사이클이 열려 있고 미도달이고 `now − started_at ≤ 20분` → `merge`(`detected_at = now`, `max_until = now + 3분`, `dry_target = None`, `merged_count += 1`). 그 밖 → 새 사이클(op 순서 `open` → `not_reached`(직전 미도달) → `vent_delay_close`(reason next_event) → `mist_gap_close`).
  - `now ≤ max_until` 동안 `peak` 갱신. `max_until` 을 지난 첫 샘플에서 `max_fixed`(`dry_target = max_humid(peak) − 12`). 도달 판정은 `dry_target` 이 있을 때만.
  - 한 샘플 안 순서: 공백 처리 → 감지(open/merge) → MAX → 도달 → 환기 지연(6h) → 환기 지연 stale(15h) → 분무 공백.
  - 분무 공백: 최근 사이클 `status ∈ {reached_in_time, reached_late}` 이고 유효 경과 ≥ 15시간이고 미발생.
  - `open.at` = 최저점 시각, 나머지 op 의 `at` = 샘플 시각(`dry_reached` = 그 샘플).
- Acceptance: `uv run pytest tests/test_humid_cycle.py -q` 통과.

- [ ] **Step 1: 실패 테스트** — 헬퍼 `feed(tracker, start, [(초, 습도), …])`(3초 간격 보간). 이름과 핵심 assertion:
  - `test_rise_detects_wet_event`: 50% 10분 → 2분에 65% → `open` 1회, `open.at` = 상승 직전 마지막 50% 샘플, `start_humid == 50`.
  - `test_rise_below_threshold_not_wet`: 50→59, 52→61(+9) → 없음.
  - `test_high_start_needs_5pp`: 64→69.5 → `open`. 64→68.5 → 없음.
  - `test_wet_needs_60_after_rise`: 40→55 → 없음.
  - `test_slow_rise_not_wet`: 50→70 을 30분 직선 → 없음.
  - `test_spike_smoothed_out`: 한 샘플만 98% → 없음.
  - `test_single_rise_plateau_no_merge`: 50→70(2분) 후 70% 10분 → `open` 1, `merge` 0.
  - `test_max_window_from_detection_and_cap`: 50→70(감지) → 감지 2분 50초 뒤 90 → `max_fixed` 1회(감지 + 3분 뒤), `dry_target == 73.0`, `peak == 90`.
  - `test_merge_within_20min_refixes`: 50→70, 64% 로 내려옴, 12분에 64→74 → `merge` 1, `open` 1, `max_fixed` 2회(58.0 → 62.0), 병합 뒤 창 안에서 `dry_reached` 없음.
  - `test_new_event_after_20min_or_dry`: 25분에 재급상승 → `open` → `not_reached`. 도달 후 10분 재급상승도 새 `open`.
  - `test_dry_reached_in_time`: MAX 70 → 5시간 뒤 58.0 → `dry_reached`(`reached_in_time`), 이후 65% 여도 유지.
  - `test_dry_and_6h_same_sample_dry_wins`: 정확히 6시간 샘플이 58.0 → `dry_reached`(`reached_in_time`), `vent_delay` 없음.
  - `test_vent_delay_at_6h`: 6시간 − 3초 없음, 6시간 `vent_delay` 1회.
  - `test_reached_late_closes_vent_delay`: 7시간 도달 → `dry_reached`(`reached_late`) + `vent_delay_close`(reason dry, at = 도달 샘플).
  - `test_vent_delay_stale_at_15h`: 15시간까지 안 마름 → `vent_delay_close`(reason stale), `mist_gap` 없음.
  - `test_offline_extends_vent_delay`: 2시간째 90분 공백 → 벽시계 7시간 30분에 `vent_delay`, `offline_sec == 5400`.
  - `test_gap_resets_rise_window`: 50% → 2분 공백 → 70% → 없음.
  - `test_reconnect_spike_needs_history`: 공백 뒤 첫 샘플들 50, 98, 98 → 없음(표본·이력 부족).
  - `test_mist_gap_after_dry_15h`: 도달한 사이클 시작 + 15시간 → `mist_gap` 1회 → 새 급상승 → `open` 다음 `mist_gap_close`.
  - `test_mist_gap_excludes_offline_on_closed_cycle`: 도달 뒤 공백 1시간 → 16시간에 `mist_gap`.
  - `test_no_cycle_no_mist_gap`: 사이클 없이 20시간 → 없음.
  - `test_restore_continues`: 시작 5시간 55분 전 open 사이클로 생성 → 5분 뒤 `vent_delay`. `vent_delay_fired=True` 면 안 냄.
  - `test_snapshot_restore_reemits`: `open` 직후 `restore_snapshot` → 다음 샘플에서 같은 `started_at` 으로 `open` 다시.
- [ ] **Step 2: 실패 확인** — ImportError.
- [ ] **Step 3: 구현** — 30초 중앙값 deque, 3분 최저값 단조 deque(같은 값이면 뒤 샘플 유지).
- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `humid: 습기 사이클 순수 상태 기계 (O-a~O-h)`

### Task 2: 리플레이 게이트 (커밋 안 함, 마이그레이션 전)

**Files:** 스크래치 `rise_fp/replay_cycles.py` — `HumidCycleTracker` 를 베타 10대 7일 원본(`rise_fp/data`, `row.ts` 시각)에 그대로.

**Context:**
- Depends on: Task 1
- Acceptance(전부 보고, 하나라도 넘으면 Task 3 전에 사용자 판단):
  - 분무 명령 세션 재현 ≥ **90%**(세션 −2분~+5분 안 `open`/`merge`).
  - 명령 없는 `open` ≤ **0.2/기기·일**(오탐 상한, 손분무 포함이라 상한만).
  - 이벤트당 `merge` 중앙값 ≤ 1.
  - 환기 지연 + 분무 공백 푸시 **기기당 하루 2회 초과 기기 0대**(넘으면 기기·원인 보고).
  - 시작 습도 구간(<60 / ≥60)별 건기 도달률·환기 지연률 — ≥60 쪽이 대부분 환기 지연이면 기획자에게 문제로만 전달(MAX−12 는 기획 규칙), 켜기 보류.
  - MAX(감지 + 3분)가 감지 후 10분 최고점보다 낮은 정도(중앙값·p90).
- [ ] **Step 1:** 실행, 결과를 이 문서 "리플레이 결과" 절에 기록하고 사용자 보고.

#### 리플레이 결과 (2026-11-03, 베타 10대 10/27~11/3, 50 기기·일, 커밋 6940f52)

| 기준 | 결과 | 판정 |
|---|---|---|
| 분무 재현 ≥ 90% | 100/107 (93%). 놓친 7건 중 3건은 1ca5e65c 의 상승 2~3%p 분무(물이 거의 안 나옴) | 통과 |
| 명령 없는 `open` ≤ 0.2/기기·일 | 30건 = 0.60. 14건이 분무 명령 0회 기기(9081f488, 손분무), 6건 1ca5e65c | **기준 잘못 잡음** — 11-03 분석에서 기획자 원안(+10%p)만으로도 0.68. 우리 규칙이 더한 것은 0.08. 손분무가 대부분으로 보임 |
| 이벤트당 병합 중앙값 ≤ 1 | 0 (최대 1) | 통과 |
| 하루 푸시 2회 초과 기기 0 | 9081f488 10/29 하루 3회(환기 지연 2 + 분무 공백 1). 나머지 49 기기·일은 ≤ 1 | **1건 초과** — 아래 시작 습도 문제와 같은 기기 |
| 시작 습도별 | < 60%: 97건, 권장 내 도달 95%, 환기 지연 1%. ≥ 60%: 22건, 권장 내 도달 64%, 미도달 23%, 환기 지연 23% | 대부분은 아님 — 다만 ≥ 60% 시작은 환기 지연이 20배 |
| MAX 부족분 | 감지 후 10분 최고 대비 중앙값 0.0%p, p90 1.1%p | 통과 |

전체 알림량: 습기 이벤트 2.46/기기·일, 환기 지연 0.12, 분무 공백 0.38 → 푸시 평균 0.5/기기·일.

### Task 3: 마이그레이션 `humid_cycles`

**Files:** Create `migrations/2026-11-03_humid_cycles.sql` · Modify `MIGRATIONS_APPLIED.md`(적용 후), `docs/DATABASE.md`

**Context:**
- Depends on: Task 2 통과
- Must know: 운영 적용은 Chrome Supabase SQL 편집기로. 적용 전 같은 이름 테이블 없음 확인 → 적용 → `BEGIN; … ROLLBACK;` 으로 authenticated 쓰기 거부 확인. 코드 배포보다 먼저.
- Acceptance: 운영에 테이블·인덱스·정책 존재, `MIGRATIONS_APPLIED.md` 기록.

```sql
-- 2026-11-03: 습기 사이클 (기획 v4 0-2·1-2·1-3·1-6, specs/stage-o-humid-cycle-v4.md Task 3)
-- 습기 이벤트 1개 = 1행. 브리지(service_role)만 쓰고, 앱은 RLS 로 본인 기기 행만 SELECT.
-- 앱 칩은 alerts(env_vent_delay·env_mist_gap)로 그리므로 Realtime publication 은 넣지 않는다.
CREATE TABLE IF NOT EXISTS public.humid_cycles (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id            UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    started_at           TIMESTAMPTZ NOT NULL,           -- 급상승 시작(3분 최저값 시각)
    start_humid          NUMERIC(5,1) NOT NULL,
    detected_at          TIMESTAMPTZ NOT NULL,           -- 마지막 (병합) 상승 감지 시각
    max_until            TIMESTAMPTZ NOT NULL,           -- MAX 측정 창 끝 = detected_at + 3분
    max_raw              NUMERIC(5,1) NOT NULL,          -- 지금까지 관측한 최고값(상한 전)
    max_humid            NUMERIC(5,1),                   -- MAX(상한 85). NULL = 측정 중
    dry_target           NUMERIC(5,1),                   -- max_humid − 12. NULL = 확정 전
    dry_reached_at       TIMESTAMPTZ,                    -- 최초 도달(이후 바뀌지 않음)
    status               TEXT NOT NULL
                         CHECK (status IN ('open', 'reached_in_time', 'reached_late', 'not_reached')),
    closed_at            TIMESTAMPTZ,                    -- not_reached/해제/스위치 끔으로 닫힌 시각
    ended_by             TEXT CHECK (ended_by IN ('next_event', 'unlinked', 'disabled')),
    offline_sec          INTEGER NOT NULL,               -- 시작 후 센서 공백 누적(6·15시간 판정에서 뺌)
    merged_count         SMALLINT NOT NULL,              -- 20분 안 재급상승 병합 횟수
    vent_delay_alert_id  UUID REFERENCES public.alerts(id) ON DELETE SET NULL,
    mist_gap_alert_id    UUID REFERENCES public.alerts(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL,
    UNIQUE (device_id, started_at)
);
CREATE INDEX IF NOT EXISTS idx_humid_cycles_device ON public.humid_cycles(device_id, started_at DESC);
ALTER TABLE public.humid_cycles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own humid_cycles select" ON public.humid_cycles;
CREATE POLICY "own humid_cycles select" ON public.humid_cycles
    FOR SELECT USING (device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid()));
```

- [ ] **Step 1:** SQL·`docs/DATABASE.md`. **Step 2:** 운영 적용·확인·롤백 테스트. **Step 3:** 기록, 커밋 `migrations: humid_cycles — 습기 사이클 기록(RLS 본인 SELECT)`.

### Task 4: DB·푸시 연결 `backend/humid_cycle_store.py` + v4 경로 호출

**Files:**
- Create: `backend/humid_cycle_store.py`
- Modify: `backend/alerts.py`(`_DeviceState.cycles` 필드, `_evaluate_v4` 호출 위치, `_stop_unlinked`), `backend/unlink_service.py`(`_close_env_alerts`), `scripts/close_v4_alerts.py`(`--cycles`), `backend/analysis_runner.py`(플래그를 `humid_cycle` 에서 import)
- Test: `tests/test_humid_cycle_store.py`, `tests/test_alerts.py`, unlink 테스트

**Context:**
- Depends on: Task 1, Task 3
- Consumes: Task 1 Produces 전부; `push_events.build_env_alert_event`/`enqueue_env_alert`; `alerts._Step`
- Produces:
  - `CYCLE_KINDS = ("env_vent_delay", "env_mist_gap")`, `TITLES`(두 문구)
  - `@dataclass CycleDeviceState`: `tracker`, `restored: bool`, `pending_open: tuple[object, list[CycleOp], float] | None`(스냅숏·전이 op·첫 실패 시각), `dirty: dict[tuple[str, str], dict]`((테이블, id) → 패치), `push_retry: dict[str, tuple[str, float, float]]`(alert_id → kind·값·첫 실패 시각)
  - `def step(c, st: CycleDeviceState, row: dict, humid: float | None, confirm_linked: Callable[[], bool]) -> None`
  - `def select_active_cycle_alerts(sb, device_uuid: str | None = None) -> list[dict]`, `def close_cycle_alert(sb, row, resolved_at: str, reason: str) -> None`, `def end_latest_cycle(sb, device_uuid: str, ended_by: str, at: str) -> None`
- Must know:
  - **호출 위치:** `_evaluate_v4` 에서 해제 확인·재페어링 처리·sensor_fault·`_flush_dirty` 다음, **Stage N `_restore`·설정 조회 전**. `humid_cycles_enabled()` 이고 `humid_cycles_since()` 가 있을 때만. 예외는 잡아서 로그. 끝난 뒤 `st.unlinked` 면 반환. 상태는 `_DeviceState.cycles` 라 재페어링·`reset_state()` 에 같이 초기화.
  - **샘플 시각:** Global Constraints 규칙(`row.ts` ±10분).
  - **전이 쓰기:** 한 `step` 결과에 `open` 이 있으면 그 사이클을 먼저 upsert(`on_conflict=device_id,started_at`) — 실패면 `restore_snapshot` 하고 `pending_open` 에 보관, 10분 동안 다음 샘플마다 같은 행 upsert 재시도(새 판정보다 먼저). 성공 뒤에 `not_reached`·`*_close` 를 쓴다. 다른 op 는 UPDATE(실패 → dirty).
  - **알림 열기:** `confirm_linked()` → 같은 kind 활성 행 조회(있으면 그 id 사용) → 없으면 INSERT(context: `rule`, `level="caution"`, `cycle_id`, `cycle_started_at`, `value`, `max_humid`, `dry_target`) → humid_cycles `*_alert_id` UPDATE → started 푸시. INSERT 실패 → `restore_snapshot`(다음 샘플 재발생). 푸시 성공 → `context.pushed_at`, 실패 → `push_retry`(10분, 재시도 직전 `confirm_linked`).
  - **알림 닫기:** `resolved_at = max(op.at, triggered_at)`, `context.closed_reason`.
  - **복원(첫 호출):** 기기 최신 사이클 1행(`started_at ≥ since`, `ended_by IS NULL`) + 활성 `CYCLE_KINDS` 행. `*_fired` = `humid_cycles.*_alert_id IS NOT NULL`. 사이클이 도달·종료 상태인데 열린 환기 지연이 있으면 닫는다(reason dry). `pushed_at` 없는 10분 안 알림은 `push_retry` 에. 재시작 공백 = 현재 `row.ts` − (telemetry 에서 `ts < row.ts`·`a_ok`·`h_a` 있는 직전 행 ts) > 60초면 `offline`; 그런 행이 없으면(보존 7일) `now_wall − updated_at`. 조회 실패면 이 샘플 보류.
  - **해제:** `_stop_unlinked`·`unlink_service._close_env_alerts` 가 두 kind 종료 + `end_latest_cycle(ended_by='unlinked')`. `close_v4_alerts.py --cycles --apply` 는 `ended_by='disabled'`.
  - 재시도 장치는 (테이블, id) 키 하나로 — Stage N `dirty` 와 같은 방식, 별도 클래스 만들지 않음.
- Acceptance: `uv run pytest -q` 전부 통과.

- [ ] **Step 1: 실패 테스트** (FakeSB + monkeypatch 실패 주입):
  - `test_open_upserts_cycle_row`: 급상승 → 1행(status open, start_humid 50.0, offline_sec 0, merged_count 0, updated_at 있음).
  - `test_open_fail_retries_same_started_at_then_closes`: 직전 미도달 사이클 + 새 급상승, 첫 upsert 실패 → 직전 사이클 `open` 유지, 다음 샘플 upsert 성공 후 직전 사이클 `not_reached`·`ended_by next_event`.
  - `test_vent_delay_alert_and_push_once`: 6시간 → alerts 1행(warning, `[환기 지연] 분무 후 건기 미도달`, context.cycle_started_at) + outbox 1건, `context.pushed_at`.
  - `test_alert_insert_lost_response_no_duplicate`: INSERT 성공 후 예외 → 다음 샘플에서 활성 행 재사용, 행 1개.
  - `test_dry_reached_resolves_vent_delay`: resolved_at = 도달 시각, `reached_late`.
  - `test_mist_gap_alert_and_close_on_new_event`: 도달 사이클 + 15시간 → `env_mist_gap` + 푸시 1회 → 새 급상승 → resolved_at = max(새 시작, triggered_at).
  - `test_restore_keeps_timers_and_no_double_open`: 5시간 59분 open 행 + 재시작 → 환기 지연 1회. `vent_delay_alert_id` 가 있으면 안 연다.
  - `test_restore_closes_leftover_vent_delay`: 도달 사이클 + 열린 환기 지연 → 복원 때 닫힘.
  - `test_restart_gap_from_prev_valid_row`: 직전 유효 행 30분 전(사이에 a_ok false 행) → offline_sec += 1800.
  - `test_reenable_no_storm`: `ended_by='disabled'` 사이클·since 이전 사이클만 있음 → 알림 없음.
  - `test_repair_starts_fresh`: 해제(최근 사이클 `ended_by='unlinked'`) → 재페어링 → 20시간 지나도 분무 공백 없음.
  - `test_burst_uses_row_ts`: 3분치 샘플이 wall 1초 안에 몰려 와도 `row.ts` 간격으로 판정.
  - `test_push_retry_confirms_linked`: 푸시 실패 → 재시도 전 해제 확인 → 해제면 재시도 안 함.
  - `test_flag_off_or_no_since_noop`: 쓰기 없음.
  - `test_unlink_closes_cycle_alerts`: 브리지·해제 API 둘 다 두 kind 종료.
  - `test_push_disabled_records_only`: alerts 행 있고 outbox 없음.
- [ ] **Step 2~4:** 실패 확인 → 구현 → 전체 통과.
- [ ] **Step 5: 커밋** — `humid: 습기 사이클 기록·환기 지연·분무 공백 알림 (HUMID_CYCLES_ENABLED)`

### Task 5: 일일·주간 분석 연동 (P9·Q10·㉕·O-f)

**Files:** Modify `backend/analysis_runner.py`, `backend/daily_analysis.py`, `backend/weekly_analysis.py` · Test 각 테스트 파일

**Context:**
- Depends on: Task 4(플래그 함수)
- Produces:
  - `daily_analysis.cycle_window(window: Window) -> Window` = (시작 − 3시간, 끝 − 3시간).
  - `_fetch_cycles(sb, device_id, window)`: 스위치 꺼짐 또는 `since` 없음 또는 `cycle_window(window)[0] < since` → `None`. 아니면 `cycle_window` 로 조회.
  - 일일 `build_humid_card`: 사이클 카드는 그날 습도 유효 수집일(`is_valid_day(buckets, window, "h_a")`)일 때만. 도달 수 = `dry_reached_at ≤ window_end` 만(`status` 분기 삭제).
  - 주간 `_cycle_stats`: `cycle_window(week_window)` 시작, 도달 컷오프 `≤ window_end`. `HUM_NO_MIST` = 습도 이벤트 chain 0 + 사이클 0.
- Must know: `env_vent_delay` 줄은 이미 `HUMID_EVENT_KINDS` 로 카드에 나온다. `env_mist_gap` 은 카드에 넣지 않는다. 02~05시에 시작한 사이클은 D 카드에, 그 환기 지연 줄(08~11시 발생)은 D+1 카드에 나온다 — 받아들이는 동작(테스트로 고정).
- Acceptance: 아래 + 전체 통과.

- [ ] **Step 1: 실패 테스트:**
  - `test_cycle_belongs_from_0500`: report 11/2 — 11/2 04:59 제외, 11/2 05:00·11/3 04:59 포함 → `wet_count == 2`.
  - `test_dry_reached_after_cutoff_not_counted`: 11/3 04:00 시작, 08:30 도달, status `reached_in_time` → `dry_reach_count 0`.
  - `test_invalid_day_no_cycle_card`: 습도 무효인 날, cycles [] → 카드 None.
  - `test_since_gate_daily` / `test_since_gate_weekly`: since 가 사이클 창 시작 뒤 → cycles None(카드 P9 이전과 같음).
  - `test_weekly_no_mist_card`: 유효 7일, 이벤트 0, cycles [] → `HUM_NO_MIST`, 문구 원문. 건조 주의 1회 있으면 `HUM_NO_MIST` 아님.
  - `test_weekly_dry_cutoff_and_0500`: 월 05:00 시작은 그 주, 04:59 는 전주. 주 끝 뒤 도달은 `{dry_time}` 에서 제외.
  - `test_vent_delay_line_split_documented`: 11/3 03:00 시작 사이클 → 11/2 카드 `wet 1`, 11/3 09:00 환기 지연 줄은 11/3 카드.
  - `test_runner_fetches_cycle_window`: 조회 gte 11/2 05:00 KST, lt 11/3 05:00 KST.
- [ ] **Step 2~4**, **Step 5: 커밋** — `analysis: 습기 사이클 카드 05:00 경계·켠 시각·주간 습기 미감지`

### Task 6: 문서·앱 계약·관측 스크립트

**Files:** Create `docs/APP_HUMID_CYCLE_2026-11-03.md` · Modify `docs/ENV.md`(두 변수), `docs/APP_ENV_ALERT_PUSH_2026-11-02.md`, `docs/APP_DAILY_ANALYSIS_2026-11-02.md`·`docs/APP_WEEKLY_ANALYSIS_2026-11-02.md`, `docs/BACKEND_HANDOFF_REPLY_ENV_ALERTS_V4_2026-11-02.md` §7(앱 §8 확정 회신), 이 스펙 상태. 메인 체크아웃의 미커밋 `reports/check_v4_alerts.py` 에 두 kind 추가(레포 밖 도구).

**Context:**
- Depends on: Task 1~5
- Must know: 앱 §8 확정 답 — kind 이름 그대로, `context.level = "caution"`, message 두 문구, payload kind 같음, 두 kind 모두 started 푸시 1회, 수신 끄기 `env_humid` 분기 그대로, 분무 공백은 "건기 도달 뒤 15시간"(아직 안 마른 사육장엔 안 나감), 환기 지연은 15시간 뒤 stale 로 닫힐 수 있음.
- Acceptance: 문서 값이 코드 상수와 같다.

- [ ] **Step 1:** 작성. **Step 2:** 커밋 `docs: 습기 사이클 앱 계약·ENV`.

## 켜는 순서 (배포 후)

1. 마이그레이션 운영 적용(Task 3) — 코드 배포 전.
2. PR 머지·배포(이관훈님). 스위치 꺼짐이라 영향 없음.
3. `ALERTS_V4_ENABLED` 가 켜진 상태에서 `HUMID_CYCLES_ENABLED=true` + `HUMID_CYCLES_SINCE=<켠 시각>` + 브리지 재시작.
4. 하루 관측: `reports/check_v4_alerts.py`(두 kind 포함)로 기기별 수 확인.
5. 롤백: `HUMID_CYCLES_ENABLED=false` + 재시작, `scripts/close_v4_alerts.py --cycles --apply`. 다시 켤 땐 새 `HUMID_CYCLES_SINCE`.

## 검증 방법

- 단위: `uv run pytest -q`.
- 리플레이 게이트: Task 2(마이그레이션 전).
- 운영 DB: Task 3 확인 쿼리(사용자 Chrome 세션).
- 코드 리뷰: 사용자가 `/code-review`.
- 실기기 화면(칩·푸시 문구): **사용자·앱 팀만 확인 가능.**

## 사용자가 정할 것

- **O-g 병합 창 5분 → 20분**(11-02 에 기획자에게 "5분"으로 물었던 것을 데이터로 넓힘) — 승인 시 확인.
- 리플레이 게이트(Task 2) 결과가 기준을 넘으면 그때.

## 이 Stage 가 풀어 주는 것

- Stage P P9: 습도 이벤트 없는 날 카드 — 05시 경계·켠 시각 반영.
- Stage Q: HUM_DELAY, HUM_NORMAL, `{dry_time}`, ㉕ `HUM_NO_MIST`.

## 변경 기록

- r1 → r2: O-a~O-h 확정, O-b "쓰지 않음"(명령 입력·`source` 컬럼 삭제), 시간 축 wall, 오프라인 = 유효 샘플 간격.
- r2 → r3(검수 반영, `reports/stage-o-v4-r2-review-final.md`): 병합 판정(최저점 ≥ `max_until`)·MAX 창 = 감지 + 3분·병합 시 재확정, 샘플 시각 `row.ts`·재시작 공백 = 직전 유효 행, open 먼저 쓰고 종료는 나중(스냅숏 되돌림·10분 재시도·UNIQUE), 푸시 `pushed_at`·해제 재확인, 복원 규칙(`*_alert_id`·`ended_by`·since·남은 알림 정리), `HUMID_CYCLES_SINCE`·습도 유효일 게이트, 주간 도달 컷오프·`HUM_NO_MIST` 조건, 분무 공백은 건기 도달 뒤만·환기 지연 15시간 stale 종료, Realtime 삭제, 리플레이 게이트를 마이그레이션 앞으로.
