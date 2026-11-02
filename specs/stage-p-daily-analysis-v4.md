# Stage P — 일일 온습도 분석 (기획 v4 2-1~2-3) Implementation Plan

> **구현 방식 (CAOF):** Critical 트랙 — Implementer 에이전트 분리(GATE 4), 별도 worktree. Steps use checkbox (`- [ ]`) syntax for tracking.
> 상태: 🚧 구현 완료(브랜치 feat/daily-analysis-v4, `ENV_REPORTS_ENABLED` 꺼짐, 마이그레이션 미적용) — 계획 승인 2026-11-02 r2 — 교차 검수 `reports/stage-opq-v4-review-final.md`. **선행: Stage N 머지** — 입력은 alerts 의 v4 이벤트(`env_*` kind, severity `critical`=경고/`warning`=주의, `context.rule='v4'`)와 `triggered_at`/`resolved_at`/`context.peak_value`·`peak_diff`·`threshold`·`chain_root`.

**Goal:** 매일 08:00~09:00(KST)에 기기별로 전날 08:00~당일 08:00 데이터를 분석해 온도·습도 분석 카드를 `env_daily_reports` 에 저장하고, 앱이 RLS 로 직접 읽게 한다.

**Architecture:** 카드 문구를 만드는 순수 함수(`backend/daily_analysis.py`)와 대상 기기·입력 조회·저장을 맡는 브리지 스레드(`backend/analysis_runner.py`). 입력은 `alerts`(v4 이벤트)와 `telemetry_30m`(주야간 평균·유효 표본 수). 문구는 서버가 렌더링해 저장(문구 정본은 서버 하나). 소급 기록되는 이벤트(Stage N D11)를 놓치지 않게 **2단계 생성**(08시대 1차, 09:05 이후 2차 재계산).

**Tech Stack:** Python 3.12, uv, supabase-py, pytest, Supabase SQL 마이그레이션

**Spec:** 노션 v4(2026-11-02 수정본) 0-1, 2-1~2-3, 5-1. 선행 `specs/stage-n-env-alerts-v4.md`.

## Global Constraints

- 일일 집계 구간: 전날 08:00 ~ 당일 08:00 (KST). 생성: 매일 08:00~09:00(2차 재계산은 09:05 이후 1회).
- 주간: 08:00~20:00, 야간: 20:00~다음 날 08:00. 하루 주야간 온도차 = |주간 평균 − 야간 평균|.
- 온도 카드: 실시간 이벤트 요약 > 주야간 온도차 통계. 이벤트 대표 타이틀 우선순위 1 고온 경고, 2 저온 경고, 3 고온 주의, 4 저온 주의. 세부 설명은 발생 시각순 전부.
- 습도 카드: 건조 주의 > 과습 주의 > 건습 사이클 이상(환기 지연) > 적정.
- 문구는 v4 2-2·2-3 표 그대로(Task 1 에 고정).
- 리포트 생성은 `ALERTS_V4_ENABLED` 와 `ENV_REPORTS_ENABLED`(기본 false) 둘 다 참일 때만 — 기획 확인 전 잠정 정책이 앱 정본으로 저장되지 않게.
- 건드리지 않는 것: Stage N 판정·푸시, `telemetry_30m` 집계 cron, 주간 분석(Stage Q), 습기 사이클 판정(Stage O).

## 결정 사항

| # | 결정 | 근거 | 기획자 고지 |
|---|---|---|---|
| P1 | 저장 `env_daily_reports`, 기기·날짜당 1행, 앱 RLS SELECT(본인 기기), Realtime | `alerts`·`telemetry_30m` 과 같은 앱 조회 방식 | 앱 핸드오프 |
| P2 | `report_date` = 집계 시작일(KST). 11/2 08:00~11/3 08:00 → `2026-11-02` | 0-1 | 앱 핸드오프 |
| P3 | 2단계 생성: **1차** = KST 08:00 이후, 해당 기기의 07:30 버킷이 집계됐거나 08:15 가 지났으면 생성(행 없을 때만). **2차** = 09:05 이후 1회 재계산 upsert(`pass=2`). 브리지가 그 시간에 죽어 있었으면 살아난 뒤 같은 날 1차·2차 순서로. 전날보다 오래된 날은 소급 안 함 | Stage N D11 소급 시각(예: 07:10 시작 과습은 08:10 INSERT)과 집계 지연 | 불필요 |
| P4 | 구간과 겹치는 이벤트 포함(`triggered_at < 구간 끝` AND (`resolved_at` 없음 OR `resolved_at > 구간 시작`)). 생성 시점에 안 끝난 이벤트는 `{end_time}` 없이 | 2-2 | 불필요 |
| P5 | 시각은 실제 시각 `HH:MM`(KST). **구간 밖** 시각만 `M/D HH:MM`(구간 안의 다음 날 새벽 시각엔 날짜를 붙이지 않음) | 이벤트가 구간 경계를 넘는 경우 — 날짜 미표기·전부 표기안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| P6 | `{temp}`/`{diff}` = chain 의 `context.peak_value`(N 이 5분마다 갱신) 기준. `peak_diff` 없으면 `|peak_value − threshold|`, `peak_value` 도 없으면 `context.value` | 진행 중·강제 종료 행 대비 | 불필요 |
| P7 | 데이터 부족: **이벤트 카드는 집계 상태와 무관하게 항상 생성**(이벤트는 실시간 관측 사실). 이벤트가 없는 날의 주야간 통계 카드는 그날이 **유효 수집일**(온도, Stage Q Q1 과 같은 기준 — `is_valid_day`)이고 주간·야간 표본이 모두 있을 때만, 아니면 카드 없음. "데이터 부족" 문구는 기획 문구가 없어 표시하지 않음 | v4 수정본에서 데이터 부족 처리 삭제 — 현재안(표본 1개면 통계)·항상 생성안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지 + 부족 문구 필요 여부 질의) |
| P8 | 주야간 평균 = 버킷 `t_a_avg` 를 `t_a_count` 로 가중평균. 판정은 **표시값**(소수 1자리 half-up) 기준 | 표시와 판정 모순 방지 | 불필요 |
| P9 | 습도 "이벤트 없는 날" 분기는 `cycles` 입력이 있을 때만(Stage O 의 `HUMID_CYCLES_ENABLED`). 없으면 그날 습도 카드 미생성 | 습기 이벤트 기획 미정 — 임시 문구·"습기 미감지" 문구 사용안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| P10 | 대상 기기: `unlinked_at IS NULL` 이고 `owner_id` 있음. 한 tick 최대 200기기, 기기 id 순 커서로 순회(실패 기기가 뒤 기기를 막지 않게) | 운영 | 불필요 |
| P11 | 같은 `chain_root` 를 가진 행(경고 → 무음 주의(D3) → 무음 재경고(D14))은 한 줄: 구간 = 첫 행 시작 ~ 마지막 행 종료, 레벨 = 경고, `{temp}` = chain 내 가장 극단 peak | Stage N D3 확정(2026-11-02) | 불필요 |
| P12 | 카드 `status`: 경고 이벤트 있으면 `danger`, 주의만 `caution`, 주야간 1.5~6 `normal` / 그 외 `caution` | alerts severity("warning"=주의)와 어휘 충돌 방지 | 앱 핸드오프 |

## 유저 체험 시뮬레이션 (11/3 아침 앱을 연 사용자)

Stage N 이 실제로 남기는 행: 고온 주의 14:00~14:30, 고온 경고 14:30~15:50(peak 29.6), 무음 주의 15:50~16:20(chain_root = 경고).
- 온도 카드 **"위험한 온도가 기록되었어요"**, 시간순 "14:00~14:30 적정 온도보다 최대 +1.8℃ 이탈", "14:30~16:20 위험 온도 29.6℃ 감지".
- 이벤트 없이 주간 25.1℃ / 야간 21.8℃(차이 3.3℃): **"주간과 야간 모두 적정 온도를 유지했어요" / "안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요"**.
- 11/2 22:00에 시작해 아침까지 이어진 건조 주의: 습도 카드 **"건조를 주의해 주세요"**, "22:00~ 적정 습도보다 최대 -9% 이탈". 08:20에 끝나면 09:05 2차 생성에서 "22:00~11/3 08:20 …"으로 갱신.
- 습도 이벤트가 없는 날: Stage O 전까지 습도 카드 없음.

## Review Focus

1. 07:10 시작·08:10 INSERT 된 과습이 2차 생성에서 전날 카드에 들어감 → Task 3 `test_second_pass_picks_backdated`.
2. 1차는 없을 때만, 2차는 정확히 1회 덮어씀 → Task 3 `test_two_pass_idempotent`.
3. UTC 23:00 = KST 08:00 경계 → Task 1 `test_window_utc_bounds`.
4. 야간 표본 0 인 날 주야간 카드 없음(0 나누기 금지) → Task 1 `test_no_night_samples_no_card`.
5. N 이 실제로 만든 행으로 카드가 맞게 나옴 → Task 1 `test_card_from_env_rules_rows`.

---

### Task 1: 카드 생성 순수 함수 `backend/daily_analysis.py`

**Files:** Create `backend/daily_analysis.py`, Test `tests/test_daily_analysis.py`

**Context:**
- Depends on: Stage N `backend/env_rules.py`(통합 테스트에서만)
- Produces:
  - `KST = timezone(timedelta(hours=9))`
  - `TEMP_EVENT_KINDS = ("env_temp_high", "env_temp_low")`, `HUMID_EVENT_KINDS = ("env_humid_low", "env_humid_high", "env_vent_delay")`
  - `def report_window(report_date: date) -> tuple[datetime, datetime]`
  - `@dataclass(frozen=True) Card(element: Literal["temp","humid"], code: str | None, status: Literal["danger","caution","normal"], title: str, lines: list[str], data: dict)` + `Card.to_json() -> dict`(모든 필드 포함)
  - `def build_temp_card(events: list[dict], buckets: list[dict], window: tuple[datetime, datetime]) -> Card | None`
  - `def build_humid_card(events: list[dict], buckets: list[dict], window: tuple[datetime, datetime], cycles: list[dict] | None = None) -> Card | None`
  - `def daynight_diff(buckets: list[dict], window) -> float | None` — Stage Q 재사용
  - `EXPECTED_SAMPLES_PER_DAY = 28800`(3초 주기), `VALID_DAY_RATIO = 0.8` 과 `def is_valid_day(buckets: list[dict], window, metric: Literal["t_a","h_a"]) -> bool` — 구간 버킷의 `{metric}_count` 합 ≥ 28800×0.8. 유효 수집일 판정의 단일 정본(Stage Q Q1 확정 2026-11-02). Stage Q 가 재사용
  - `def fmt_temp(x: float) -> str`(소수 1자리 half-up), `def fmt_humid(x: float) -> str`(정수 half-up)
- Must know (문구 원문 고정):
  - 온도 이벤트 타이틀: 고온 경고·저온 경고 `위험한 온도가 기록되었어요`, 고온 주의 `높은 온도가 기록됐어요`, 저온 주의 `낮은 온도가 기록됐어요`
  - 온도 줄: 경고 `{start}~{end} 위험 온도 {temp}℃ 감지`, 고온 주의 `{start}~{end} 적정 온도보다 최대 +{diff}℃ 이탈`, 저온 주의 `{start}~{end} 적정 온도보다 최대 -{diff}℃ 이탈`. 진행 중이면 `{start}~ 위험 온도 …`
  - 주야간(표시값 기준 판정): `< 1.5` → `주간과 야간 온도가 거의 일정했어요` / `건강한 생체 리듬을 위해 주야 온도 차이 3~5℃를 권장해요`; `1.5 이상 6 이하` → `주간과 야간 모두 적정 온도를 유지했어요` / `안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요`; `> 6` → `주간과 야간의 온도차가 너무 커요` / `주간·야간 평균 편차 {diff}℃. 건강한 생체 리듬을 위해 주야 온도 차이 3~5℃를 권장해요`
  - 습도 타이틀: 건조 `건조를 주의해 주세요`, 과습 `과습을 주의해 주세요`, 환기 지연 `건습 사이클이 건강하지 않아요`
  - 습도 줄: 과습 `{start}~{end} 적정 습도보다 최대 +{diff}% 이탈`, 건조 `{start}~{end} 적정 습도보다 최대 -{diff}% 이탈`, 환기 지연 `{start}에 분무 후 사육장이 충분히 마르지 않음` — `{start}` 는 `context.cycle_started_at`(Stage O 가 기록), 없으면 그 줄 생략
  - `cycles` 가 주어지고 습도 이벤트가 없을 때(Stage O 이후): 사이클 ≥ 1 이고 도달 ≥ 1 → `건습 사이클이 건강하게 유지됐어요` / `습기 {wet_count}번 , 건기 {dry_reach_count}번이 기록됐어요`(원문 공백 그대로), 사이클 0 → `습기가 감지되지 않았어요` / `분무를 잊지 않으셨는지 확인해 주세요`
  - P4·P5·P6·P8·P11·P12 는 모듈 내부 함수.
  - `data`: 이벤트 id·chain 목록, `day_avg`, `night_avg`, `diff_daynight`(이벤트 있는 날에도 계산 가능하면 넣음).
- Acceptance:
  1. `uv run pytest tests/test_daily_analysis.py -v` PASS.
  2. 리플레이 덤프: 베타 7일 원본 → Stage N `env_rules` 리플레이로 만든 이벤트 + `telemetry_30m` 대용 30분 집계 → 기기·일별 카드 출력(줄 수 최대, None 포함 여부, 카드 종류 분포). 결과를 사용자에게 보고 후 Task 2 진행.

- [ ] **Step 1: 실패 테스트**

```python
def test_window_utc_bounds():                    # report_window(date(2026,11,2)) == (2026-11-01T23:00Z, 2026-11-02T23:00Z)
def test_temp_priority_and_order():              # 고온 주의 14:00~14:30 + 고온 경고 14:30~15:50 → title "위험한 온도가 기록되었어요", 줄 순서 14:00, 14:30
def test_chain_merged_line():                    # 경고 14:30~15:50(peak 29.6) + 주의(chain_root=경고) 15:50~16:20 → "14:30~16:20 위험 온도 29.6℃ 감지" 1줄
def test_ongoing_event_no_end():                 # resolved_at None → "14:00~ 위험 온도 29.6℃ 감지"
def test_event_overlapping_both_days():          # 11/2 07:50~08:20 이벤트 → 11/1 창 "07:50~11/2 08:20 …", 11/2 창 "11/2 07:50~08:20 …"
def test_cross_day_time_format():                # 11/1 창에서 11/1 22:00 시작·11/2 08:20 종료 → "22:00~11/2 08:20 …", 11/2 03:00 종료면 "22:00~03:00 …"
def test_peak_fallbacks():                       # peak_diff 없음 → |peak_value−threshold|, peak_value 도 없음 → context.value 사용, "None" 문자열 없음
def test_daynight_buckets():                     # 주간 25.1 / 야간 21.8 → "주간과 야간 모두 적정 온도를 유지했어요", data.diff_daynight == 3.3
def test_daynight_weighted_by_count():           # (26.0, 600)+(20.0, 60) 주간 → 25.45
def test_daynight_display_based_thresholds():    # 1.449→"거의 일정", 1.45→"모두 적정"(표시 1.5), 6.04→"모두 적정"(표시 6.0), 6.05→"너무 커요" + "주간·야간 평균 편차 6.1℃..."
def test_no_night_samples_no_card():             # 이벤트 없음, 야간 count 합 0 → None
def test_events_shown_without_buckets():         # 버킷 0개 + 고온 경고 이벤트 1 → 이벤트 카드 생성 (P7)
def test_invalid_day_no_daynight_card():         # 이벤트 없음 + 하루 t_a_count 합 23,039 → None, 23,040 → 주야간 카드 (P7)
def test_humid_priority_dry_over():              # 과습 + 건조 → "건조를 주의해 주세요", 2줄 시간순
def test_humid_no_event_no_cycles_none():        # 이벤트 없음, cycles=None → None (P9)
def test_humid_cycles_healthy_and_none():        # cycles 3개(도달 2) → "건습 사이클이 건강하게 유지됐어요" / "습기 3번 , 건기 2번이 기록됐어요"; cycles [] → "습기가 감지되지 않았어요"
def test_card_json_has_element_code_status():    # to_json 키 == {element, code, status, title, lines, data}, status ∈ {danger,caution,normal}
def test_card_from_env_rules_rows():             # Stage N 유저 체험 시나리오를 env_rules 로 재생해 얻은 Action 들을 alerts 행으로 변환 → 위 시뮬레이션 1번 카드와 일치
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_daily_analysis.py -v` → ImportError
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 리플레이 덤프(Acceptance 2)** — 결과 요약을 커밋 메시지 본문에
- [ ] **Step 6: 커밋** — `"analysis: v4 일일 분석 카드 생성(온도 이벤트 요약·주야간 온도차, 습도 이벤트 요약)"`

### Task 2: 마이그레이션 `env_daily_reports`

**Files:** Create `migrations/2026-11-XX_env_daily_reports.sql`, Modify `MIGRATIONS_APPLIED.md`(적용 후)

**Context:**
- Depends on: 없음
- Must know:
  - **Step 0:** 기기 해제 후 재페어링 시 `devices` 행을 재사용하는지 `backend/unlink_service.py`·페어링 경로에서 확인. 재사용이면 RLS 를 "본인 기기 + `generated_at >= 현재 소유 시작 시각`"으로 바꾸는 안을 사용자에게 보고하고 결정 후 진행.
  - 아래 선언 그대로. 쓰기는 service_role(브리지)만.

```sql
CREATE TABLE IF NOT EXISTS public.env_daily_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    report_date   DATE NOT NULL,                 -- 집계 시작일(KST): report_date 08:00 ~ 다음 날 08:00
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    temp_card     JSONB,                         -- Card.to_json() | NULL = 미생성
    humid_card    JSONB,
    pass          SMALLINT NOT NULL DEFAULT 1,   -- 1 = 08시대 1차, 2 = 09:05 이후 재계산
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, report_date)
);
CREATE INDEX IF NOT EXISTS idx_env_daily_reports_device ON public.env_daily_reports(device_id, report_date DESC);
ALTER TABLE public.env_daily_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own env_daily_reports select" ON public.env_daily_reports;
CREATE POLICY "own env_daily_reports select" ON public.env_daily_reports
    FOR SELECT USING (device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid()));
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.env_daily_reports;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
```

- Acceptance(적용 후): ① 본인 JWT SELECT 성공 ② 다른 사용자 JWT SELECT 0행 ③ authenticated JWT INSERT 거부 ④ service_role upsert 성공 ⑤ 앱 Realtime 구독으로 INSERT 수신 ⑥ `MIGRATIONS_APPLIED.md` 기록.

- [ ] **Step 0: 재페어링 확인** → **Step 1: SQL** → **Step 2: `uv run pytest tests/test_migration_coverage.py -q`** → **Step 3: 커밋** `"migrations: env_daily_reports — 일일 분석 카드 저장(RLS 본인 SELECT)"` → **Step 4(사용자): 적용·Acceptance·기록**

### Task 3: 생성 스레드 `backend/analysis_runner.py` + 브리지 등록

**Files:** Create `backend/analysis_runner.py`, Modify `backend/mqtt_bridge_main.py:63-85`, Test `tests/test_analysis_runner.py`

**Context:**
- Depends on: Task 1, Task 2
- Produces: `class AnalysisRunner(interval_sec: float = 300.0)` — `.start()`, `.stop()`, `.tick(now: datetime) -> int`; `def generate_daily(sb, device: dict, report_date: date, now: datetime, pass_no: int) -> bool`; `def reports_enabled() -> bool`(두 플래그)
- Must know:
  - `reports_enabled()` 거짓이면 tick 0.
  - 1차/2차 규칙은 P3. 07:30 버킷 확인: `telemetry_30m` 에 `bucket = window_end - 30분` 행 존재.
  - 입력 조회: `alerts` — `device_id`, `kind in TEMP_EVENT_KINDS + HUMID_EVENT_KINDS`, `context->>rule='v4'`, P4 겹침. chain 합치기를 위해 겹치는 행의 `chain_root` 가 가리키는 행도 함께. `telemetry_30m` — `bucket >= window_start`, `bucket < window_end`.
  - `cycles` = `HUMID_CYCLES_ENABLED`(Stage O 소유) 참이면 `humid_cycles` 조회 결과, 아니면 None.
  - 저장 `upsert(on_conflict="device_id,report_date")`, 1차는 없을 때만, 2차는 `pass=1` 인 행만 덮어씀. 두 카드 None 이어도 행 저장.
  - 대상·커서는 P10. 예외는 기기 단위로 잡고 로그.
- Acceptance: `uv run pytest tests/test_analysis_runner.py -v` PASS, `uv run pytest -q` 전체 PASS.

- [ ] **Step 1: 실패 테스트** (FakeSB)

```python
def test_disabled_flags_noop():                  # ENV_REPORTS_ENABLED 미설정 → 0
def test_tick_before_8am_noop():                 # 07:59 KST → 0
def test_first_pass_waits_for_last_bucket():     # 08:05, 07:30 버킷 없음 → 0; 08:16 → 생성
def test_second_pass_picks_backdated():          # 1차 후 07:10 시작 과습 행 INSERT → 09:06 tick → humid_card 에 반영, pass==2
def test_two_pass_idempotent():                  # 09:06 tick 두 번 → 두 번째 0, 행 1개
def test_skips_unlinked_and_ownerless():
def test_cursor_rotation():                      # 201기기, 1번 기기 항상 예외 → 두 tick 안에 201번 기기 생성
def test_device_error_isolated():
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현·브리지 등록** → **Step 4: 전체 PASS** → **Step 5: 커밋** `"analysis: 일일 분석 생성 스레드 — 2단계(08시 1차·09:05 재계산)"`

### Task 4: 문서 — 앱 핸드오프·DB·ENV·스펙 목록

**Files:** Create `docs/APP_DAILY_ANALYSIS_2026-11-XX.md`, Modify `docs/DATABASE.md`, `docs/ENV.md`(`ENV_REPORTS_ENABLED`), `specs/README.md`
- Must know: ① 카드 JSON 예시(이벤트 날/주야간 날/진행 중/chain) ② `report_date`·`pass` 의미(09:05 이후 내용이 바뀔 수 있음) ③ 카드 NULL 의미(P7·P9) ④ Realtime 구독 예시 ⑤ status 어휘(danger/caution/normal)와 alerts severity 차이.
- [ ] 작성 → 컬럼명·필드명 grep 확인 → 커밋 `"docs: v4 일일 분석 앱 핸드오프 + DB/ENV 문서"`

## 기획 확인이 필요한 것

- P7 "데이터 부족" 안내 문구가 필요한지(현재 카드 없음).
