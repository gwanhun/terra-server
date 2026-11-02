# Stage Q — 주간 온습도 분석 + 예외 (기획 v4 3-1~3-3, 4-1) Implementation Plan

> **구현 방식 (CAOF):** Critical 트랙 — Implementer 에이전트 분리(GATE 4), 별도 worktree.
> 상태: ✅ 계획 승인 (2026-11-02, r2 + 잠정 결정 확정). 구현 대기 — 교차 검수 `reports/stage-opq-v4-review-final.md`. **선행: Stage N, Stage P 머지.** HUM_DELAY·HUM_NORMAL 은 Stage O 의 `HUMID_CYCLES_ENABLED` 가 켜진 뒤 활성.

**Goal:** 가장 최근에 끝난 주(월요일 08:00 ~ 다음 월요일 08:00, KST)에 대해 기기별 온도·습도 주간 카드(대표 케이스 1개 + 그 케이스의 세부 문구)와 예외 안내를 `env_weekly_reports` 에 저장한다.

**Architecture:** 카드 생성 순수 함수 `backend/weekly_analysis.py` + Stage P `AnalysisRunner` 에 주간 생성 추가. 입력은 `alerts`(env_* 이벤트·chain·peak)와 `telemetry_30m`(유효 수집일·누락률·일별 주야간 차이·주 평균 온도). **일일 리포트에 의존하지 않는다**(검수 반영 — 일일 누락이 주간을 망치지 않게).

**Tech Stack:** Python 3.12, uv, supabase-py, pytest, Supabase SQL

**Spec:** 노션 v4(2026-11-02 수정본) 0-1, 3-1~3-3, 4-1, 5-2. 선행 `specs/stage-n-env-alerts-v4.md`, `specs/stage-p-daily-analysis-v4.md`.

## Global Constraints

- 집계 구간: 지난주 월요일 08:00 ~ 이번 주 월요일 08:00(KST). 생성: 월요일 08:00~09:00 이 목표, 놓치면 그 주 안에 복구 생성. UI 표시 기간 월~일(앱).
- 최소 수집: 7일 중 유효 수집일 3일 이상. 미만이면 그 지표 카드 대신 ERR_NO_DATA 안내.
- 전주 대비: 분석 대상 주와 그 직전 주 모두 유효 수집일 7일일 때만. 첫 주는 생략.
- 대표 케이스 1개, 그 케이스의 세부 문구만. 카테고리 순서 [1] 이벤트 요약 → [2] 전주 대비 → [3] 패턴 분석. 세부 문구는 데이터가 없으면 그 줄만 생략.
- 온도 우선순위: TEMP_DANGER → TEMP_CAUTION → TEMP_DIFF_HIGH → TEMP_DIFF_TOO_LOW / TEMP_DIFF_TOO_HIGH → TEMP_NORMAL.
- 습도 우선순위: HUM_DELAY → HUM_DRY / HUM_OVER / HUM_MIXED → HUM_UNSTABLE → HUM_NORMAL.
- "이번 주 / 전주" = 분석 대상 주 / 그 직전 주(기획자 댓글 확인).
- 생성은 Stage P `reports_enabled()`(두 플래그) 참일 때만.
- 건드리지 않는 것: Stage N 판정, Stage P 일일 카드, `telemetry_30m` cron.

## 결정 사항

| # | 결정 | 근거 | 기획자 고지 |
|---|---|---|---|
| Q1 | **유효 수집일**(온도 `t_a_count`, 습도 `h_a_count` 각각): 그날(08~08) 유효 표본 합 ≥ **23,040**(3초 주기 예정 28,800개의 80%) | 이전 v4 기획 추천안 "예정 수집 데이터의 80% 이상" 그대로. 베타 7일 실측(47기기·일): 버킷당 중앙값 591/600, 데이터는 꽉 차거나 통째로 비는 형태라 버킷 규칙·표본 규칙 모두 46/47 유효 — **확정(2026-11-02)** | 필요(확정 사항 고지) |
| Q2 | **누락률**(지표별) = 1 − 구간 유효 표본 합 / 예정 표본 수. 예정 = (구간 끝 − max(구간 시작, `devices.created_at`)) ÷ 3초. 예정 0 이하면 None(판정 안 함) | 이전 v4 추천안 "주간 전체 예정 수집 데이터 기준" + 신규 기기 보정(수요일 설치 기기가 첫 주부터 28% 누락으로 잡히지 않게). Q1 과 같은 표본 단위. 베타 실측 6~77% — **확정(2026-11-02)** | 필요(확정 사항 고지) |
| Q3 | ERR_NO_DATA: 지표별로 유효 수집일 < 3 이면 그 지표 카드 없이 `{code:"ERR_NO_DATA", metric}` 안내 | 4-1 | 불필요 |
| Q4 | ERR_DISCONNECT: 지표별로 유효 수집일 ≥ 3 이고 누락률 ≥ 50% 면 카드는 그대로 만들고 `{code:"ERR_DISCONNECT", metric}` 안내를 함께 저장 | 4-1 에 대체인지 병기인지 없음. 기획이 "유효 3일 이상이면 주간 분석 생성"을 따로 정했고, 그 3일의 경고를 숨기지 않기 위해 — 대체안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| Q5 | 횟수 = 구간과 **겹치는** 이벤트 chain 수(`chain_root` 같은 행은 1건, chain 의 최고 레벨로 분류). Stage N D3 무음 주의·D14 재경고는 chain 에 흡수돼 따로 세지 않음 | Stage N D3 확정, 플래핑으로 횟수가 부풀지 않게 | 불필요(N D3 고지에 포함) |
| Q6 | `{max_deviation}` = 구간 내 경고 chain 들의 peak diff 최댓값(사용자 기준 대비, Stage P P6 대체 규칙 동일) | 3-2 | 불필요 |
| Q7 | TEMP_DANGER 패턴 분석(`{time_label}`): **경고 chain 이 시작한 1시간대**(2026-11-02 변경 — 걸친 시간대를 전부 세면 하루 가까이 이어진 경고가 거의 모든 시간대를 채워 "0시~1시"가 뽑히는 것을 리플레이에서 확인)를 날짜별 1회씩 세어 같은 시간대가 서로 다른 3일 이상이면 `{H}시~{H+1}시 사이`(23시는 `23시~0시`, 동률이면 이른 시간대). 그게 없으면, 경고가 있었던 날이 3일 이상이고 경고가 전부 주간(08~20시)이면 `주간 시간`, 전부 야간이면 `야간 시간`. 둘 다 아니면 그 줄 생략 | 수정본에서 "동일 시간대 3일 이상"이 삭제되고 "반복 패턴"만 남음 → 삭제 전 3일 기준 + 기획자 `{time_label}` 예시(주간 시간·야간 시간·10시~11시 사이) — 1시간대만 쓰는 안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| Q8 | 주 평균 온도 = 버킷 `t_a_avg` 의 `t_a_count` 가중평균. `{diff_prev}` = |이번 주 − 전주|, 판정·표시 모두 소수 1자리 half-up | P8 과 동일 | 불필요 |
| Q9 | `{diff_daynight}` = 유효 수집일(Q1, 온도)인 날들의 일별 주야간 차이(`daily_analysis.daynight_diff`) 평균. 하나도 없으면 주야간 케이스 판정 불가 → TEMP_NORMAL 도 안 냄 | 이전 추천안 "날짜별 주야간 온도차의 평균값" | 불필요 |
| Q10 | `cycles` 입력이 None 이면(Stage O 이전) HUM_DELAY·HUM_NORMAL 판정 제외 → 다른 케이스가 없으면 습도 카드 없음 | 습기 이벤트 미정 | 필요 |
| Q11 | `{dry_time}` = 구간 안에서 시작해 건기 목표에 도달한(정시·지연) 사이클의 평균 소요 분, half-up, 표기 `N시간 M분`(M=0 이면 `N시간`). HUM_NORMAL 원문 `{dry_time}시간` 의 단위 중복은 `{dry_time}` 으로 고쳐 렌더링 | 5-2 예시 "6시간 20분, 6시간", HUM_DELAY 문구도 같은 형식 전제 — 숫자만 넣는 안과 비교 후 **확정(2026-11-02)** | 필요(문서 오류 고지) |
| Q12 | HUM_NORMAL 은 사이클 ≥ 1 이고 도달 ≥ 1 일 때만(값은 Stage O 확정 시 재검토). `n_delay` = 구간과 겹치는 `env_vent_delay` 수 | 사이클 0개에서 "건강" 판정 방지 | 불필요 |
| Q13 | 저장 `env_weekly_reports`, 기기·주당 1행(`week_start` = 집계 시작 월요일), 앱 RLS SELECT, Realtime | P1 과 동일 | 앱 핸드오프 |
| Q14 | 카드 `code` = 케이스 코드, `status`: TEMP_DANGER·HUM_DELAY `danger`, 나머지 이상 케이스 `caution`, TEMP_NORMAL·HUM_NORMAL `normal` | Stage P Card 공유 | 앱 핸드오프 |
| Q15 | 보정값 변경 주의 처리(구 ERR_CALIBRATION_CHANGED)는 하지 않음 | 수정본에서 삭제 | 불필요 |

## 유저 체험 시뮬레이션 (2026-11-09 월요일 아침, 대상 주 11/2~11/9)

- 고온 경고 chain 2개(사용자 MAX 대비 최대 +4.1℃), 둘 다 14시대·서로 다른 날 → 온도 카드 **"주간 중 위험 온도가 감지되었어요"** / "총 2회 위험 온도 발생. 적정 온도보다 최대 4.1℃ 이탈" (3일 미만이라 패턴 줄 없음).
- 이벤트 없음, 유효일 주야간 차이 평균 1.2℃ → **"주·야간 온도차가 거의 없었어요"** / "주야간 평균 온도차가 1.2℃로 작아요. 3~5℃ 차이를 권장해요".
- 건조 주의 4회·과습 1회 → 습도 카드 **"사육장이 건조한 날이 많았어요"** / "총 4회 건조 주의가 발생. 사육장에 수분을 보충해 주세요".
- 수요일 08:00 등록, 이후 끊김 없음(유효 5일, 누락률 0%) → 정상 분석, 전주 대비 없음.
- 유효 2일 → **"주간 분석을 위한 데이터를 모으고 있어요" / "최소 3일의 데이터가 필요해요"**.
- 월요일 하루 종일 브리지 장애 → 화요일 복구 직후 그 주 리포트 생성.

## Review Focus

1. 월 07:30~08:00 버킷이 지난주에 속함 → Task 1 `test_week_window_buckets`.
2. 첫 주(직전 주 데이터 없음) 전주 대비 생략·에러 없음 → Task 1 `test_first_week_no_prev`.
3. HUM 케이스 경계 → Task 1 `test_hum_case_boundaries`.
4. 화요일 복구 생성, 재생성 중복 없음 → Task 3 `test_weekly_recovery_tuesday`, `test_weekly_idempotent`.
5. 플래핑 chain 이 1건으로 셈 → Task 1 `test_chain_counts_once`.

---

### Task 1: 주간 카드 순수 함수 `backend/weekly_analysis.py`

**Files:** Create `backend/weekly_analysis.py`, Test `tests/test_weekly_analysis.py`

**Context:**
- Depends on: Stage P `backend/daily_analysis.py`(`KST`, `Card`, `daynight_diff`, `fmt_temp`)
- Produces:
  - `def week_window(week_start: date) -> tuple[datetime, datetime]`
  - `def valid_days(buckets, window, metric: Literal["t_a","h_a"]) -> list[date]` (Q1) — 날짜별로 Stage P `is_valid_day` 호출(기준값 정본은 P 상수)
  - `def missing_rate(buckets, window, metric, registered_at: datetime) -> float | None` (Q2, 상수는 Stage P `EXPECTED_SAMPLES_PER_DAY` 에서 3초 주기 유도)
  - `@dataclass(frozen=True) WeeklyResult(temp_card: Card | None, humid_card: Card | None, notices: list[dict])` — notice = `{"code","metric","title","body"}`
  - `def build_weekly(events: list[dict], buckets: list[dict], prev_buckets: list[dict] | None, window, registered_at: datetime, cycles: list[dict] | None = None) -> WeeklyResult`
- Must know (문구 원문 고정, 원문의 이중 공백은 하나로):
  - 온도 타이틀: TEMP_DANGER `주간 중 위험 온도가 감지되었어요` / TEMP_CAUTION `적정 온도를 벗어난 경우가 많았어요` / TEMP_DIFF_HIGH 상승 `지난주보다 평균 온도가 높아졌어요`, 하락 `지난주보다 평균 온도가 낮아졌어요` / TEMP_DIFF_TOO_LOW `주·야간 온도차가 거의 없었어요` / TEMP_DIFF_TOO_HIGH `주·야간 온도차가 컸어요` / TEMP_NORMAL `주·야간 온도차가 안정적으로 유지되었어요`
  - 온도 세부: TEMP_DANGER [1] `총 {n_danger}회 위험 온도 발생. 적정 온도보다 최대 {max_deviation}℃ 이탈`, [3] `{time_label}에 온도 이탈이 집중되었어요` / TEMP_CAUTION [1] `총 {n_temp_caution}회 적정 온도 범위를 벗어났어요` / TEMP_DIFF_HIGH [2] `지난주 대비 평균 온도가 {diff_prev}℃ 상승했어요`(하락이면 `하락했어요`) / TEMP_DIFF_TOO_LOW [3] `주야간 평균 온도차가 {diff_daynight}℃로 작아요. 3~5℃ 차이를 권장해요` / TEMP_DIFF_TOO_HIGH [3] `주야간 평균 온도차가 {diff_daynight}℃로 커요. 3~5℃ 내외를 권장해요` / TEMP_NORMAL [3] `주야간 평균 온도차가 {diff_daynight}℃로 안정적이에요`
  - 판정(표시값 기준): TEMP_DANGER 경고 chain ≥ 1, TEMP_CAUTION 주의 chain ≥ 4, TEMP_DIFF_HIGH ≥ 3.0, TOO_LOW < 1.5, TOO_HIGH > 6.0
  - 습도 타이틀: HUM_DELAY `사육장 환기가 지연되는 경향이 보여요` / HUM_DRY `사육장이 건조한 날이 많았어요` / HUM_OVER `사육장이 습한날이 많았어요` / HUM_MIXED `건조와 과습이 반복됐어요` / HUM_UNSTABLE `습도 이탈이 반복됐어요` / HUM_NORMAL `건습 사이클이 건강하게 유지되었어요`
  - 습도 세부: HUM_DELAY `총 {n_delay}회 환기 지연이 발생. 건기 도달까지 평균 {dry_time}이 걸렸어요` / HUM_DRY `총 {n_dry}회 건조 주의가 발생. 사육장에 수분을 보충해 주세요` / HUM_OVER `총 {n_over}회 과습 주의가 발생. 환기 상태를 점검해 주세요` / HUM_MIXED `건조 주의 {n_dry}회, 과습 주의 {n_over}회 발생. 분무량과 환기 상태를 함께 점검해 주세요` / HUM_UNSTABLE `총 {n_hum_caution}회 습도 이탈 발생. 일정한 루틴으로 분무를 관리해 주세요` / HUM_NORMAL `건기 도달 평균 시간 {dry_time}으로 건습 사이클을 유지했어요`(Q11)
  - 판정: HUM_DELAY 환기 지연 ≥ 2, HUM_DRY 건조 ≥ 3 & 과습 ≤ 2, HUM_OVER 과습 ≥ 3 & 건조 ≤ 2, HUM_MIXED 둘 다 ≥ 3, HUM_UNSTABLE 합 ≥ 3 & 둘 다 ≤ 2. 건조·과습 = `env_humid_low`·`env_humid_high` 만(다른 kind 섞지 않음).
  - 예외 문구: ERR_NO_DATA `주간 분석을 위한 데이터를 모으고 있어요` / `최소 3일의 데이터가 필요해요`, ERR_DISCONNECT `주간 데이터 수집이 원활하지 않았어요` / `센서 전원과 Wi-Fi 연결 상태를 확인해 주세요`
  - 이벤트 입력은 Stage P 와 같은 kind 목록 + chain 처리(P11) 재사용.
- Acceptance:
  1. `uv run pytest tests/test_weekly_analysis.py -v` PASS.
  2. 리플레이 덤프: 베타 7일(1주) 데이터로 기기별 Q1 유효일 수·누락률·케이스를 출력해 사용자에게 보고(Q1 통과율 확인용).

- [ ] **Step 1: 실패 테스트**

```python
def test_week_window_buckets():                  # week_start=2026-11-02 → (11/1 23:00Z, 11/8 23:00Z); 11/9 07:30 KST 버킷 포함, 08:00 제외
def test_valid_days_sample_rule():               # 하루 t_a_count 합 23,040 → 유효, 23,039 → 무효, h_a 는 따로 셈
def test_missing_rate_from_registration():       # 수요일 08:00 등록, 이후 표본 꽉 참 → 0.0; 등록 이후 절반 → 0.5; 구간 끝 뒤 등록 → None
def test_err_no_data_per_metric():               # t_a 유효 2일·h_a 5일 → temp_card None + notice(ERR_NO_DATA, metric="t_a"), humid 정상
def test_err_disconnect_alongside():             # t_a 유효 4일·누락률 0.55 → temp_card 있음 + notice(ERR_DISCONNECT, metric="t_a")
def test_temp_danger_card():                     # 경고 chain 2개 peak diff 3.2, 4.1 → lines[0]=="총 2회 위험 온도 발생. 적정 온도보다 최대 4.1℃ 이탈", status "danger", code "TEMP_DANGER"
def test_temp_danger_pattern_3days():            # 14시대 걸친 경고 chain 이 서로 다른 3일 → "14시~15시 사이에 온도 이탈이 집중되었어요"; 23시대 → "23시~0시 사이…"
def test_temp_danger_pattern_day_night():        # 월 13시·수 15시·금 11시 경고 → "주간 시간에 온도 이탈이 집중되었어요"; 21시·02시·23시(다른 3일) → "야간 시간…"; 주간·야간 섞임 → 줄 없음
def test_chain_counts_once():                    # 경고→무음 주의→무음 재경고 chain 1개 + 주의 3개 → n_danger 1, TEMP_DANGER
def test_overlapping_prior_event_counted():      # 지난주 일요일 시작해 이번 주 화요일 끝난 경고 → TEMP_DANGER
def test_temp_caution_4():                       # 주의 chain 3 → 다음 케이스, 4 → TEMP_CAUTION
def test_temp_diff_high_needs_full_weeks():      # 두 주 유효 7일, 25.0 vs 21.8 → 높아졌어요 + "지난주 대비 평균 온도가 3.2℃ 상승했어요"; 한 주 6일 → 생략
def test_first_week_no_prev():                   # prev_buckets None → 전주 대비 판정 없음
def test_daynight_cases():                       # 유효일 diff 평균 1.2 → TOO_LOW, 6.3 → TOO_HIGH, 3.0 → TEMP_NORMAL "주야간 평균 온도차가 3.0℃로 안정적이에요"
def test_daynight_only_valid_days():             # 무효일(버킷 10개)의 diff 9.0 은 평균에서 제외
def test_hum_case_boundaries():                  # (dry,over)=(2,1)→UNSTABLE, (3,2)→DRY, (2,3)→OVER, (3,3)→MIXED, (1,1)→None
def test_hum_cycles_none_excludes_delay_normal():# cycles=None → 환기 지연 2회여도 HUM_DELAY 아님
def test_hum_delay_and_dry_time():               # cycles 4개(도달 380·360분, 미도달 2) + env_vent_delay 2 → HUM_DELAY "총 2회 환기 지연이 발생. 건기 도달까지 평균 6시간 10분이 걸렸어요"
def test_hum_normal_requires_cycles():           # cycles [] → HUM_NORMAL 아님(카드 None), cycles 2개 도달 2 → HUM_NORMAL "건기 도달 평균 시간 …으로 …"
def test_dry_time_format():                      # 380분 → "6시간 20분", 360분 → "6시간"
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: PASS** → **Step 5: 리플레이 덤프** → **Step 6: 커밋** `"analysis: v4 주간 분석 카드·예외(유효 수집일·누락률·케이스 우선순위)"`

### Task 2: 마이그레이션 `env_weekly_reports`

**Files:** Create `migrations/2026-11-XX_env_weekly_reports.sql`, Modify `MIGRATIONS_APPLIED.md`(적용 후)

```sql
CREATE TABLE IF NOT EXISTS public.env_weekly_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,                 -- 집계 시작 월요일(KST): week_start 08:00 ~ +7일 08:00
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    temp_card     JSONB,                         -- Card.to_json() | NULL
    humid_card    JSONB,
    notices       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{code,metric,title,body}]
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, week_start)
);
CREATE INDEX IF NOT EXISTS idx_env_weekly_reports_device ON public.env_weekly_reports(device_id, week_start DESC);
ALTER TABLE public.env_weekly_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own env_weekly_reports select" ON public.env_weekly_reports;
CREATE POLICY "own env_weekly_reports select" ON public.env_weekly_reports
    FOR SELECT USING (device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid()));
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.env_weekly_reports;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
```

- Acceptance: Stage P Task 2 의 ①~⑥ 과 동일(재페어링 정책도 P 결정 따름).
- [ ] **Step 1: SQL** → **Step 2: `uv run pytest tests/test_migration_coverage.py -q`** → **Step 3: 커밋** → **Step 4(사용자): 적용·확인·기록**

### Task 3: `AnalysisRunner` 주간 생성

**Files:** Modify `backend/analysis_runner.py`, Test `tests/test_analysis_runner.py`(추가)

**Context:**
- Depends on: Task 1, 2, Stage P Task 3
- Produces: tick 마다 `week_start = 가장 최근 월요일(KST, 08:00 이후 기준) - 7일` 리포트가 없는 기기에 `generate_weekly(sb, device, week_start, now) -> bool`. 월요일이 아니어도 동작(복구), 그보다 이전 주는 소급 안 함.
- Must know: 입력 조회 — `alerts`(구간과 겹침, Stage P 의 kind 목록 + `env_vent_delay`, `context->>rule='v4'`, chain 원본 행 포함), `telemetry_30m`(이번 주 + 직전 주, `t_a_avg, t_a_count, h_a_count`), `cycles` = `HUMID_CYCLES_ENABLED` 참이면 `humid_cycles`(구간 안 시작), 아니면 None. `devices.created_at` = `registered_at`. upsert `on_conflict="device_id,week_start"`. 같은 커서·200기기 상한(P10).
- [ ] **Step 1: 실패 테스트**

```python
def test_weekly_monday_after_8():                # 월 07:59 → 0(지지난 주 리포트 있음), 월 08:01 → 생성
def test_weekly_recovery_tuesday():              # 월요일 tick 없음, 화 10:00 → 그 주 리포트 생성
def test_weekly_no_older_backfill():             # 2주 전 리포트 없음 → 생성 안 함
def test_weekly_idempotent():                    # 두 번 tick → 행 1개
```

- [ ] **Step 2~5:** 실패 확인 → 구현 → `uv run pytest -q` PASS → 커밋 `"analysis: 주간 분석 생성(최근 완료 주, 장애 복구 포함)"`

### Task 4: 문서 — 앱 핸드오프·DB 문서·스펙 목록

**Files:** Create `docs/APP_WEEKLY_ANALYSIS_2026-11-XX.md`, Modify `docs/DATABASE.md`, `specs/README.md`
- Must know: 카드 JSON 예시(케이스별 1개), `notices` 표시 위치·`metric`(Q3·Q4), `week_start` 의미, 카드 NULL 의미(Q3·Q9·Q10), status/code 어휘.
- [ ] 작성 → 컬럼명 grep 확인 → 커밋 `"docs: v4 주간 분석 앱 핸드오프 + DB 문서"`

## 기획 확인이 필요한 것

- (없음 — Q 잠정 결정은 2026-11-02 사용자와 모두 확정. 기획자 고지 대상은 각 행의 "고지" 열)
