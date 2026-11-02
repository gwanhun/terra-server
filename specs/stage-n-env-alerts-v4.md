# Stage N — 실시간 온습도 이벤트·푸시 (기획 v4 1-1~1-6) Implementation Plan

> **구현 방식 (CAOF):** Critical 트랙 — Implementer 에이전트 분리(GATE 4), 별도 worktree. Steps use checkbox (`- [ ]`) syntax for tracking.
> 상태: 🚧 구현 완료(브랜치 feat/env-alerts-v4, 플래그 꺼짐) — 계획 승인 2026-11-02 r2 — 교차 검수 `reports/stage-n-env-alerts-v4-review-final.md`
> **구현 순서: Task 1 → 3 → 2 → 4.** settings 검증은 별도 Stage N2 로 분리(앱 확인 후 배포).

**Goal:** 브리지가 telemetry 마다 온·습도를 v4 규칙(2분 진입 / 5분 회복 / 1시간 추가 푸시)으로 판정해 `alerts` 에 v4 전용 이벤트를 기록하고, 앱 푸시용 이벤트를 `push_outbox` 에 적재한다. 전체가 `ALERTS_V4_ENABLED`(기본 false) 뒤에 있어 앱 확인 전에는 운영 동작이 바뀌지 않는다.

**Architecture:** 판정 규칙은 DB·시계를 모르는 순수 상태 기계 `backend/env_rules.py`, `backend/alerts.py` 는 설정 로드·이벤트 복원·DB 기록·푸시 적재만. 진입/회복 타이머는 브리지 메모리(monotonic), 진행 중 이벤트·푸시·peak 는 `alerts` 행에 둬서 재시작해도 이어진다. 플래그가 꺼져 있으면 기존 판정(`alert_*` 컬럼 기반)이 그대로 돈다.

**Tech Stack:** Python 3.12, uv, supabase-py, pytest (`tests/fake_sb.py`)

**Spec:** 노션 "비바나트 앱 (사육장, 캠)" › v4. 최종 정리 중 (2026-11-02 수정본) 1-1~1-6. 푸시 판정 방식은 기획자 위임("더 안정적인 방법이 있으면 고지 후 변경"). 시뮬레이션 근거: 베타 10대 7일, 2분/5분 기기당 하루 3.97건(추가 푸시 포함), 과습 주의 12건/7일.

## Global Constraints

- 온도 경고(danger): 15℃ 미만 / 29℃ 초과. 온도 주의(caution): 사용자 MIN 미만 / MAX 초과. 디폴트 MIN 18℃, MAX 26℃.
- 습도 건조 주의: 사용자 MIN 미만(2분). 과습 주의: 사용자 MAX 초과 **1시간 연속 유지**. 디폴트 MIN 40%, MAX 80%.
- 적정 범위 = MIN ≤ 값 ≤ MAX (경계 포함). 15.0℃·29.0℃ 는 경고 아님.
- 상태 진입 2분 유지, 상태 회복 5분 유지, 유지 도중 조건이 깨지면 타이머 초기화.
- 같은 이벤트가 최초 푸시 후 1시간 지속되면 추가 푸시 1회. 종료 후 재발생은 새 이벤트·새 푸시.
- 주의 → 경고: 경고 조건 2분 유지 시 기존 이벤트 종료 후 경고 이벤트 생성·푸시.
- 판정 값: 메인 센서 A(`t_a`, `h_a`), 기기가 보정값을 적용해 보낸 값 그대로.
- 푸시는 타이틀 문구만(v4 1-5). 수신자 = `devices.owner_id`.
- service_role 소유자 필터(CLAUDE.md): 앱 요청 경로가 아닌 **브리지 내부 처리**라 `device_id` 단일 조건으로 조회한다(기존 `alerts.py`·`push_events.py` 와 동일). 사용자 데이터를 응답으로 내보내지 않음.
- 단일 브리지 인스턴스 전제(기존 러너들과 동일).
- 건드리지 않는 것: 펌웨어 발 알림 경로(`handle_alert`)와 그 행, `device.action.*` 푸시, `alerts`·`push_outbox` 스키마(마이그레이션 없음), 습기 사이클(Stage O), 일일·주간 분석(Stage P·Q), settings 검증(Stage N2).

## 결정 사항

| # | 결정 | 근거 | 기획자 고지 |
|---|---|---|---|
| D1 | 유지 판정 = 기획 원안 "연속 유지 + 도중 깨지면 초기화" | 시뮬레이션상 대안과 하루 0.1~0.4건 차이 | 불필요 |
| D2 | 샘플 간격 60초 초과, 센서 오류(`a_ok=false`), 값 없음·NaN 이면 해당 요소의 진입·회복 타이머 초기화 | 공백을 "유지됐다"고 볼 근거 없음. 일시정지·연속 간주안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| D3 | 경고 → 주의 완화: 경고 조건 아님이 5분 유지 + 그 시점 값이 같은 쪽 주의 범위면 경고 종료 + 주의 이벤트(푸시 없음, 추가 푸시도 없음). 이 주의 행은 `context.deescalated_from = 경고 alert id`, `context.chain_root = 그 경고의 chain_root(없으면 경고 id)`. **분석(Stage P·Q)에서는 별도 이벤트로 세지 않고 원래 경고에 합침**(경고 구간 = 경고 시작 ~ 이 주의의 종료) | 1-4 에 없음. 나아지는 방향 — 푸시 안 함 / 경고 유지 / 분석 분리 안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| D4 | 회복은 시간(5분)만, 값 여유 없음 | 기획 의도로 충분 | 불필요 |
| D5 | 반대쪽 전환: 진행 중 이벤트와 반대쪽 조건이 2분 유지되면 기존 종료 + 반대쪽 이벤트·푸시 | 문서에 없음 | 불필요 |
| D6 | v4 전용 kind: `env_temp_high`, `env_temp_low`, `env_humid_low`, `env_humid_high`. `severity`: 주의 `warning`, 경고 `critical`. `context.rule = "v4"` | 펌웨어 `handle_alert` 가 `temp_high` 등을 그대로 INSERT 함(handlers.py:1267) — 이름을 공유하면 복원·종료가 섞임 | 앱 핸드오프 |
| D7 | 경과 시간은 `time.monotonic()`, DB 기록 시각은 wall clock(`datetime.now(UTC)`) | 서버 시계 보정이 타이머에 섞이지 않게 | 불필요 |
| D8 | 설정 60초 TTL 캐시(브리지). 행 없음·NULL → 디폴트. 조회 실패 → 직전 값 유지(없으면 그 샘플 평가 보류). 저장값이 v4 범위 밖이면 범위로 clamp, MIN ≥ MAX 면 그 요소 디폴트. 기준이 바뀌면 그 요소의 타이머 초기화 | 현재 캐시는 API 프로세스에서만 비워져 브리지가 옛 값 사용 | 불필요 |
| D9 | 푸시 이벤트 `env.alert.triggered`, `phase` = `started`/`reminder`, 스위치 `PUSH_ENV_ALERTS_ENABLED`(기본 false) | 앱 계약 전 적재 금지 | 앱 핸드오프 |
| D10 | `context.threshold` 는 경고 이벤트도 포함해 항상 **사용자** MIN/MAX(넘은 쪽). `{diff}`·`peak_diff` = 측정값과 그 기준의 차이 절댓값(경고 타이틀엔 diff 미표시, 주간 `{max_deviation}` 이 사용). 온도 소수 1자리 **올림**, 습도 정수 **올림**, 최소 0.1℃ / 1%p | 0 으로 표시 방지, banker's rounding 회피 — 반올림안과 비교 후 **확정(2026-11-02)**. 실시간 `%p` / 일일 `%` 단위 불일치는 문서대로 렌더링하고 기획자에게 통일 여부 질의 | 필요(확정 사항 고지 + 단위 질의) |
| D11 | `triggered_at` = 조건 연속 성립 시작 시각, `resolved_at` = 적정(또는 완화 조건) 연속 유지 시작 시각. 전환(주의→경고, D5) 시 기존 close 시각 = 새 open 시각. peak 는 진입 대기 구간 포함, 진행 중 바뀌면 5분마다 `context.peak_value` UPDATE, 종료 시 `peak_value`·`peak_diff` 확정 | Stage P 의 `{start_time}~{end_time}`, `{temp}`, `{diff}` 가 실제 이탈 구간을 가리켜야 함 — 판정 시각안과 비교 후 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| D12 | 오프라인·센서 오류가 이어지는 동안 진행 중 이벤트는 열린 채 유지(종료는 적정 5분 관측 시에만). 기기 해제(`unlinked_at`)가 확인되면 평가 중단 + 활성 v4 이벤트 종료 | 관측 없이 회복을 단정할 수 없음 — 오프라인 즉시 종료·일정 시간 후 종료안과 비교 후 **확정(2026-11-02)**. 카드에 미관측 구간 표시는 하지 않음(기획 문구 없음) | 필요(확정 사항 고지) |
| D13 | 경고 조건이 성립 중이면 주의 진입 보류(정상→주의→1분 뒤 경고의 이중 푸시 방지) | 검수 지적 | 불필요 |
| D14 | 무음 완화(D3)로 열린 주의에서 30분 안에 다시 경고로 가면 경고 이벤트는 열되 푸시 생략(`context.push_suppressed = true`, `context.chain_root` 승계 — 분석에서 한 건으로 묶임) | 28.9↔29.1 플래핑 시 7분 주기 경고 푸시 방지. 적정 복귀 없이 경고↔주의만 오가는 건 같은 위험 상황 — 30분 기준 **확정(2026-11-02)** | 필요(확정 사항 고지) |
| D15 | 추가 푸시는 `pushed_at`+1시간 경과 **그리고 현재 샘플이 이벤트 조건을 만족**할 때만 | 회복 대기 중 "아직 위험" 오발송 방지 | 불필요 |
| D16 | `pushed_at` 은 outbox 적재 성공 시에만 기록. 적재 실패(스위치 OFF·소유자 없음 제외)는 10분간 다음 샘플에서 재시도(event_id 멱등) | 기록과 적재 사이 유실 최소화 | 불필요 |

**전이 우선순위(한 step 에 전이 1개):** ① 활성 없음: 경고 진입(2분) > 주의 진입(2분, 과습 60분; D13) ② 활성 주의: 경고 진입(2분) > 반대쪽 진입(D5) > 회복(5분) ③ 활성 경고: 반대쪽 진입(D5) > 회복(5분) > 완화(D3). 전이가 없을 때만 추가 푸시 판정(D15).

## 유저 체험 시뮬레이션 (MAX 26℃, 푸시 ON)

1. 14:00 히터 과열로 26.3℃. 14:02까지 계속 26℃ 초과 → **14:02 푸시 "[고온 주의] 적정 온도보다 +0.3℃"**. 이벤트 시작 시각은 14:00.
2. 14:05 26.0℃(적정)로 잠깐 내려왔다가 14:07 다시 26.2℃ → 적정 5분 미달, 같은 이벤트, 푸시 없음.
3. 14:30 29.4℃, 14:32까지 유지 → 고온 주의 종료(14:30), **14:32 푸시 "[고온 경고] 온도가 너무 높아 개체가 위험할 수 있어요"**.
4. 15:32 여전히 29℃ 초과 → **추가 푸시 1회**. 15:32에 이미 28℃로 내려와 있었다면 안 보냄.
5. 15:50 팬으로 27.5℃ → 15:55까지 29℃ 이하 유지 → 경고 종료, 고온 주의로 조용히 전환.
6. 16:20 25.5℃ → 16:25까지 적정 → 종료(16:20). 푸시 없음.
7. 17:00 다시 26.4℃ 2분 → 새 이벤트, **새 푸시**.

습도: 분무로 85%(MAX 80) → 1시간 넘게 연속 80% 초과일 때만 **과습 주의**. 40% 밑으로 2분 → **건조 주의**.

## Review Focus

1. 재시작 직후 중복 없음(started·reminder 모두) → Task 2 `test_restore_no_duplicate_push`.
2. MAX 26→28 변경 후 60초 안에 새 기준 적용, 타이머 초기화 → Task 2 `test_settings_ttl_refresh`.
3. 회복 대기 중(적정 4분째) 1시간이 지나도 추가 푸시 없음 → Task 1 `test_no_reminder_while_recovering`.
4. 펌웨어가 보낸 `temp_high` 활성 행이 v4 복원·종료에 안 섞임 → Task 2 `test_firmware_alert_untouched`.
5. `ALERTS_V4_ENABLED` 꺼짐: 기존 판정 그대로, v4 행·푸시 0 → Task 2 `test_flag_off_legacy_path`.

---

### Task 1: 순수 판정 상태 기계 `backend/env_rules.py`

**Files:**
- Create: `backend/env_rules.py`
- Test: `tests/test_env_rules.py`
- Create(검증용, 커밋 안 함): `/private/tmp/claude-501/-Users-baek-terra-server/8e3fbce2-fec1-40a9-96de-39b0ae58d89a/scratchpad/push_sim/replay_env_rules.py` (같은 폴더 `raw/*.csv` 입력, `sim.py` 의 `load()` 재사용)

**Context:**
- Depends on: 없음
- Produces:
  - 상수 `WARN_TEMP_LOW=15.0`, `WARN_TEMP_HIGH=29.0`, `DEFAULT_TEMP_MIN=18.0`, `DEFAULT_TEMP_MAX=26.0`, `DEFAULT_HUMID_MIN=40.0`, `DEFAULT_HUMID_MAX=80.0`, `TEMP_SET_RANGE=(15.0, 29.0)`, `HUMID_SET_RANGE=(35.0, 85.0)`, `ENTRY_SEC=120`, `RECOVERY_SEC=300`, `OVER_HUMID_ENTRY_SEC=3600`, `REMINDER_SEC=3600`, `GAP_SEC=60`, `REDANGER_SILENCE_SEC=1800`, `PEAK_FLUSH_SEC=300`
  - `@dataclass(frozen=True) Thresholds(temp_min, temp_max, humid_min, humid_max)` + `Thresholds.default()` + `Thresholds.sanitize(raw: dict) -> Thresholds`(D8 clamp·디폴트 규칙)
  - `Level = Literal["caution", "danger"]`
  - `@dataclass ActiveEvent(kind: str, level: Level, started_wall: datetime, pushed_mono: float | None, reminded: bool, silent: bool, peak: float, alert_id: str | None = None, silent_since_mono: float | None = None)`
  - `@dataclass(frozen=True) Action(op: Literal["open","close","remind","peak"], kind: str, level: Level, push: bool, value: float | None, threshold: float | None, at_mono: float, peak: float | None = None, closes_alert_id: str | None = None)`
  - `class ElementTracker(element: Literal["temp","humid"])`: `.active`, `.restore(ev: ActiveEvent | None, now_mono: float)`, `.step(now_mono: float, value: float | None, th: Thresholds) -> list[Action]`, `.reset_timers()`, `.mark_pushed(now_mono: float)`, `.rollback_open()`(직전 open 취소: 활성 해제, 타이머는 유지)
  - `def title(kind: str, level: Level, value: float, threshold: float | None) -> str`
- Must know:
  - DB·시계 호출 금지. 시각은 인자로만.
  - 조건별 "연속 성립 시작 monotonic 시각"을 들고 `now - start >= 기준초` 면 유지. D2 사유면 해당 트래커 타이머 전부 초기화.
  - 전이 우선순위는 위 표 그대로. close+open 은 Action 2개, close 에 `closes_alert_id`(이전 active 의 id) 를 담는다 — 트래커는 open 후 새 이벤트를 가리킴.
  - open 은 `push=True` 로 내고(D14 해당 시 False), 트래커는 `mark_pushed` 가 불릴 때만 `pushed_mono` 설정(D16).
  - remind: 전이 없음 + `pushed_mono` 있음 + `not reminded` + `now - pushed_mono >= REMINDER_SEC` + 현재 값이 이벤트 조건 만족(D15).
  - peak: 진입 대기 구간부터 추적(open 시 대기 구간 극값으로 초기화), 바뀐 뒤 `PEAK_FLUSH_SEC` 지나면 `op="peak"` Action.
  - `title` 문구(정확히):
    - env_temp_low/danger `[저온 경고] 온도가 너무 낮아 개체가 위험할 수 있어요`
    - env_temp_high/danger `[고온 경고] 온도가 너무 높아 개체가 위험할 수 있어요`
    - env_temp_high/caution `[고온 주의] 적정 온도보다 +{diff}℃`
    - env_temp_low/caution `[저온 주의] 적정 온도보다 -{diff}℃`
    - env_humid_low/caution `[건조 주의] 적정 습도보다 -{diff}%p`
    - env_humid_high/caution `[과습 주의] 적정 습도보다 +{diff}%p`
    - diff 규칙은 D10.
- Acceptance:
  1. `uv run pytest tests/test_env_rules.py -v` 전부 PASS.
  2. 리플레이: 베타 7일 원본(위 폴더 `raw/*.csv`)을 `ElementTracker` 로 돌려 기기당 하루 푸시(started+reminder)와 D14 생략 건수, 회복 중 remind 0건을 출력. 시뮬레이션(3.97건/일)과 ±1건 이내가 아니면 Task 3 진행 전 사용자에게 보고.

- [ ] **Step 1: 실패 테스트** — 3초 간격 샘플 헬퍼 `feed(tr, [(t, v)...], th)`:

```python
def test_caution_after_2min_continuous():       # 26.3 → t=120 에 open(env_temp_high, caution, push=True), 이전 []
def test_boundaries_exact():                    # 26.0·18.0 은 적정, 29.0·15.0 은 경고 아님, 80.0·40.0 은 적정
def test_jitter_on_boundary_resets_entry():     # 26.1,26.2,26.0 반복 10분 → open 없음
def test_recovery_needs_5min_in_range():        # 25.5 299초 → 없음, 300초 → close(at_mono == 적정 시작)
def test_brief_return_keeps_event():            # 적정 4분 후 재이탈 → close 없음
def test_caution_to_danger():                   # 주의 중 29.4 120초 → [close(closes_alert_id=이전 id, at == 새 open.at), open(danger, push=True)]
def test_low_side_symmetric():                  # 17.5 → 저온 주의, 14.5 → 저온 경고, 완화·회복 대칭
def test_danger_from_normal_skips_caution():    # 24 → 30 120초 → open(danger) 1개
def test_caution_held_while_danger_building():  # 24 → 27(60초) → 30 → t=120 주의 open 없음, 30 진입 2분째 danger open 1개 (D13)
def test_danger_deescalates_silently():         # 경고 중 27.5 300초 → [close, open(caution, push=False)]
def test_redanger_within_30min_silent():        # 무음 주의 10분 뒤 29.4 120초 → open(danger, push=False) (D14), 31분 뒤면 push=True
def test_reminder_once_after_1h():              # mark_pushed 후 3600초 이탈 지속 → remind 1개, 이후 없음
def test_no_reminder_while_recovering():        # 3590초에 25℃ → 3600~3889 remind 없음 (D15)
def test_no_reminder_without_mark_pushed():     # mark_pushed 안 부름 → remind 없음 (D16)
def test_gap_resets_timers():                   # 26.3 100초 → 61초 공백 → 26.3 100초 → 없음, 120초째 open
def test_gap_exactly_60_keeps_timers():         # 60초 공백 → 타이머 유지
def test_none_or_nan_resets_timers():           # None·NaN 1건 → 초기화
def test_humid_over_needs_1h_continuous():      # 85% 3599초 → 없음, 3600초 → open(env_humid_high); 1800초에 80.0 한 건 → 다시 3600초 필요
def test_humid_dry_2min_and_recovery():         # 39% 120초 → open, 45% 300초 → close
def test_opposite_side_switch():                # 고온 주의 중 17℃ 120초 → [close, open(env_temp_low, caution, push=True)]
def test_peak_includes_entry_window_and_flush():# 26.3(0초)·27.8(60초)·26.5 → open.peak==27.8; 이후 28.1 → 300초 뒤 op="peak" 28.1
def test_restore_keeps_event():                 # restore(caution, pushed_mono=now-3500, reminded=False) → 100초 뒤 이탈 중이면 remind
def test_sanitize_thresholds():                 # max 30 → 29, min 10 → 15, humid min 85·max 85 → 디폴트 40/80
def test_titles():                              # title(env_temp_high,caution,26.04,26.0)=="[고온 주의] 적정 온도보다 +0.1℃"
                                                # title(env_humid_high,caution,80.4,80.0)=="[과습 주의] 적정 습도보다 +1%p"
                                                # title(env_humid_low,caution,33.4,40.0)=="[건조 주의] 적정 습도보다 -7%p"
                                                # title(env_temp_low,danger,14.2,None)=="[저온 경고] 온도가 너무 낮아 개체가 위험할 수 있어요"
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_env_rules.py -v` → ImportError
- [ ] **Step 3: 구현** — 모듈 docstring 에 전이 우선순위 표와 D1~D5, D13~D15 요약.
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 리플레이(Acceptance 2)** — 결과를 커밋 메시지 본문에 3줄로.
- [ ] **Step 6: 커밋** — `"alerts: v4 온습도 판정 상태 기계(env_rules) — 2분 진입·5분 회복·1시간 추가 푸시"`

### Task 3: `backend/push_events.py` — `env.alert.triggered` 이벤트

**Files:**
- Modify: `backend/push_events.py`
- Test: `tests/test_push_events.py` (추가)

**Context:**
- Depends on: 없음 (Task 2 가 소비)
- Produces:
  - `EVENT_ENV_ALERT = "env.alert.triggered"`
  - `build_env_alert_event(*, alert_id: str, kind: str, level: str, phase: Literal["started","reminder"], title: str, value: float, threshold: float | None, device_id: str, device_key: str | None, device_meta: dict | None) -> dict | None`
  - `env_alerts_push_enabled() -> bool` — `PUSH_ENV_ALERTS_ENABLED` 가 `true/1/yes/on` 이고 기존 `_enabled()` 도 참
  - `enqueue_env_alert(event: dict) -> Literal["queued","duplicate","disabled","failed"]`
- Must know:
  - 봉투는 `build_command_event` 와 동일: `schema_version`, `event_id=f"alert:{alert_id}:{phase}"`, `type`, `occurred_at`, `user_id`, `payload`.
  - `payload`: `alert_id, device_id, device_key, device_name, enclosure_id, kind, level(caution|danger), phase, title, value, threshold`.
  - `user_id` = `device_meta["owner_id"]`, 없으면 None.
  - 기존 `enqueue()` 의 bool 은 그대로 두고 새 함수가 결과를 4가지로 구분(23505 → duplicate). Task 2 의 D16 재시도는 `failed` 일 때만.
- Acceptance: `uv run pytest tests/test_push_events.py -v` PASS.

- [ ] **Step 1: 실패 테스트**

```python
def test_env_event_shape():                     # event_id=="alert:a1:started", type=="env.alert.triggered", payload.level=="caution", payload.title==입력
def test_env_event_reminder_id():               # "alert:a1:reminder"
def test_env_event_none_without_owner():        # owner_id None → None
def test_enqueue_env_alert_disabled_default():  # 플래그 미설정 → "disabled", push_outbox INSERT 0
def test_enqueue_env_alert_queued_and_dup():    # 플래그 on + ingest 설정 → "queued", 같은 event_id 재적재 → "duplicate"
def test_enqueue_env_alert_failed():            # INSERT 예외(23505 아님) → "failed"
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현**(모듈 docstring "발행 범위"에 env 알림 단락) → **Step 4: PASS** → **Step 5: 커밋** `"push: env.alert.triggered 이벤트 — PUSH_ENV_ALERTS_ENABLED 기본 꺼짐"`

### Task 2: `backend/alerts.py` — v4 경로 연결 (플래그 뒤)

**Files:**
- Modify: `backend/alerts.py` (기존 판정을 `_evaluate_legacy` 로 이름만 옮기고 유지, v4 경로 추가)
- Modify: `backend/mqtt/handlers.py:1027-1033` (호출에 `device_key=device_id_text`)
- Create(운영용): `scripts/close_v4_alerts.py` — 롤백 시 활성 v4 행 일괄 종료(`--dry-run` 기본)
- Test: `tests/test_alerts.py` (신규)

**Context:**
- Depends on: Task 1, Task 3
- Produces: `evaluate_telemetry(device_uuid: str, telemetry_row: dict, device_key: str | None = None) -> None`(공개 시그니처 호환), `reset_settings_cache()`, `reset_state()`(테스트용)
- Must know:
  - **Step 0(구현 전 확인):** 운영 DB 에서 `alerts.kind`/`severity` 에 CHECK 제약이 없는지, `push_outbox.event_id` UNIQUE 인지 SQL Editor 로 확인하고 결과를 커밋 메시지에 남김(코드상 initial_schema.sql:139-151 은 제약 없음, push_events.enqueue 의 23505 처리는 UNIQUE 전제).
  - `ALERTS_V4_ENABLED` 가 `true/1/yes/on` 이 아니면 `_evaluate_legacy` 만 실행(현행 그대로). 켜져 있으면 v4 만 실행하고 legacy 는 안 돈다. sensor_fault 처리는 두 경로 공통으로 기존 그대로.
  - 설정(D8): `device_settings.target_temp_min/max, target_humid_min/max` → `Thresholds.sanitize`. 기기별 `(만료 monotonic, Thresholds)` 60초. 바뀌었으면 해당 요소 `reset_timers()`.
  - 복원(기기 첫 v4 평가): `alerts` 에서 `device_id`, `resolved_at IS NULL`, `kind IN ('env_temp_high','env_temp_low','env_humid_low','env_humid_high')`(접두어 매칭 금지 — Stage O 의 `env_vent_delay` 등은 대상 아님), `context->>rule = 'v4'` 조회. 요소별 최신 1행 복원, 나머지는 `resolved_at=now` 로 종료(아래 강제 종료 규칙). `context.pushed_at`(wall) → `pushed_mono = now_mono - (now_wall - pushed_at)`, `context.reminded_at` 있으면 `reminded=True`, `context.peak_value` → peak, `context.deescalated_from` 있으면 silent=True. 조회 **실패**면 그 기기는 복원 완료로 표시하지 않고 그 샘플 평가 보류(다음 샘플 재시도).
  - 구 브리지 판정이 열어 둔 행(`kind IN (temp_high,temp_low,humid_low)` 이고 `context` 에 `threshold` 키, `rule` 없음)은 v4 첫 평가 때 종료. 펌웨어 행(그 외)은 건드리지 않음.
  - Action 처리:
    - `open` → INSERT `{device_id, kind, severity: caution→"warning"/danger→"critical", message: title(...), triggered_at: wall(at_mono), context: {rule:"v4", level, value, threshold, diff, peak_value, deescalated_from?, push_suppressed?}}`. **INSERT 실패면 `rollback_open()`** 후 종료(다음 샘플 재판정). 성공 시 id 를 active 에. push=True 면 `build_env_alert_event(phase="started")` → `enqueue_env_alert`: `queued`/`duplicate` → `mark_pushed` + `context.pushed_at` UPDATE / `disabled` 또는 이벤트 None → 기록만 / `failed` → 10분간 다음 샘플마다 재시도(D16).
    - `close` → UPDATE `resolved_at: wall(at_mono)`, `context` 에 `peak_value`, `peak_diff` 병합 (`closes_alert_id` 기준). 실패 시 기기별 재시도 목록에 넣고 다음 샘플에서 재시도.
    - `remind` → 적재 성공 시에만 `context.reminded_at` UPDATE, 트래커 `reminded=True`.
    - `peak` → `context.peak_value` UPDATE.
  - **강제 종료**(중복 행 정리, 기기 해제 D12, `scripts/close_v4_alerts.py`)도 `context.peak_value` 가 있으면 `peak_diff = |peak_value − threshold|` 를 함께 기록 — 분석이 "+None" 을 만들지 않게.
    - `wall(at_mono) = now_wall - (now_mono - at_mono)`.
  - `a_ok=false` → 두 트래커에 None. `a_ok=true` 여도 `t_a`/`h_a` 가 숫자가 아니거나 NaN 이면 해당 요소만 None.
  - 기기 해제(D12): `device_meta`(handlers.device_meta, 지연 임포트)에 `unlinked_at` 이 있으면 활성 v4 행 종료 + 트래커 삭제 + 평가 안 함 — `device_meta` select 에 `unlinked_at` 추가 필요(handlers.py:234).
- Acceptance: `uv run pytest tests/test_alerts.py tests/test_mqtt_handlers.py -v` PASS, 이어서 `uv run pytest -q` 전체 PASS.

- [ ] **Step 0: 스키마 확인** (위)
- [ ] **Step 1: 실패 테스트** (FakeSB, `time.monotonic`·`datetime.now` monkeypatch):

```python
def test_flag_off_legacy_path():                 # 플래그 off, alert_temp_high=25, t_a=26 → 기존처럼 temp_high INSERT, env_* 0, outbox 0
def test_defaults_when_no_settings_row():        # 플래그 on, 설정 행 없음, 26.3 120초 → env_temp_high/severity "warning" INSERT 1, triggered_at == 0초 시각
def test_user_threshold_used():                  # target_temp_max=28 → 27℃ 10분 → INSERT 없음
def test_settings_out_of_range_clamped():        # target_temp_max=35 → 29 로 clamp → 29.5℃ 는 danger 로만 처리
def test_settings_ttl_refresh():                 # max 26 진입 후 28 로 변경, 61초 뒤부터 27℃ 300초 → resolved
def test_settings_fetch_failure_keeps_last():    # 조회 예외 → 직전 Thresholds 유지
def test_restore_no_duplicate_push():            # 활성 env_temp_high(pushed_at·reminded_at 있음) + 이탈 2시간 → INSERT 0, outbox 0
def test_restore_query_failure_defers():         # 복원 조회 예외 → 그 샘플 INSERT 0, 다음 샘플에서 복원 후 중복 없음
def test_restore_multiple_active_keeps_latest(): # 같은 요소 활성 2행 → 최신 1행 복원, 나머지 resolved
def test_legacy_bridge_rows_closed():            # context{t_a,threshold} 인 temp_high 활성 → v4 첫 평가 때 resolved
def test_firmware_alert_untouched():             # context 에 threshold 없는 temp_high 활성(펌웨어) → 그대로
def test_open_insert_failure_rolls_back():       # INSERT 예외 → 다음 샘플에서 다시 open 시도, outbox 에 alert:None 없음
def test_push_disabled_no_pushed_at():           # PUSH_ENV_ALERTS_ENABLED off → context.pushed_at 없음, 1시간 뒤 remind 0
def test_push_failed_retries_10min():            # enqueue "failed" → 다음 샘플 재시도, 성공 시 pushed_at 기록
def test_caution_to_danger_rows():               # 주의 행 resolved_at == 경고 행 triggered_at
def test_close_writes_times_and_peak():          # 26.3(t=0)→27.8→25℃(t=1000) → resolved_at==t1000, peak_value 27.8, peak_diff 1.8
def test_sensor_fault_regression():              # a_ok=false → sensor_fault INSERT 1, 복구 시 resolve (기존 동작)
def test_unlinked_device_stops():                # device_meta.unlinked_at 있음 → 활성 env 행 resolved, 이후 INSERT 0
def test_handler_to_outbox_integration():        # handlers.handle_telemetry 로 3초 샘플 41개(26.3℃) → alerts env_temp_high 1, push_outbox 1(플래그 on)
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현**(모듈 docstring 의 평가 규칙표를 v4·legacy 병기로) → **Step 4: 전체 PASS** → **Step 5: `scripts/close_v4_alerts.py`**(dry-run 출력 확인) → **Step 6: 커밋** `"alerts: v4 실시간 판정(ALERTS_V4_ENABLED 기본 꺼짐) — 디폴트·설정 TTL·재시작 복원·추가 푸시"`

### Task 4: 문서 — 앱 핸드오프·스펙 목록

**Files:**
- Create: `docs/APP_ENV_ALERT_PUSH_2026-11-XX.md`
- Modify: `docs/APP_INTEGRATION.md:368-389`(§4.3 kind·severity 표에 env_* 추가), `docs/ENV.md`(`ALERTS_V4_ENABLED`, `PUSH_ENV_ALERTS_ENABLED`), `specs/README.md`(Stage N 행)

**Context:**
- Depends on: Task 2, 3
- Must know: 핸드오프에 ① `env.alert.triggered` 예시(started·reminder) ② `alerts` 의 env_* kind × severity 표, 기존 kind 와 공존 ③ 앱이 결정할 것: 타이틀을 서버 `title` 그대로 쓸지, 사용자 푸시 수신 OFF 처리(서버는 항상 적재, 거르기는 앱 서버라는 가정) ④ 스위치 켜는 순서와 각 단계에서 앱 화면에 보이는 변화.
- Acceptance: 문서의 kind·필드명이 코드 상수와 일치(`grep -n "env_temp_high\|env.alert.triggered" docs backend`).

- [ ] **Step 1: 작성** → **Step 2: grep 확인** → **Step 3: 커밋** `"docs: v4 온습도 알림 앱 핸드오프 + ENV 플래그"`

## 배포·검증·롤백

1. 머지·배포: 두 플래그 모두 꺼짐 → 운영 동작 변화 없음(legacy 판정 그대로).
2. 앱 팀에 핸드오프 전달(담당: 사용자). 앱이 env_* kind 표시 준비 완료 회신 → `ALERTS_V4_ENABLED=true` + 브리지 재시작. 이때부터 앱 Realtime 에 env_* 이벤트가 보임, 푸시는 아직 없음.
3. 하루 관측: v4 이벤트 open 수(무음 제외)와 "적재됐을 푸시 수(started+reminder)"를 로그로 집계. 기기당 하루 6건을 넘는 기기가 있으면 원인 확인 후 진행.
4. 앱 서버 수신부 준비 회신 → `PUSH_ENV_ALERTS_ENABLED=true`.
5. 롤백: `ALERTS_V4_ENABLED=false` + 재시작 → legacy 복귀. 활성 v4 행은 `uv run python scripts/close_v4_alerts.py --apply` 로 종료. 대기 중 outbox 는 `PUSH_ENV_ALERTS_ENABLED=false` 로 신규 적재만 막고 기존 행은 워커가 보냄(필요 시 수동 `status='abandoned'`).
6. 기획자 고지: D2, D3, D10, D11, D12, D14.

## 확인 못 한 것

- 앱이 현재 `alerts` 의 kind/severity 를 어떻게 표시하는지(앱 코드 미보유).
- 사용자 푸시 수신 설정이 앱 서버에 있는지.
- 운영 DB 의 `alerts` CHECK 제약 유무(Task 2 Step 0 에서 확인).

---

# Stage N2 — 목표 온습도 설정 검증을 v4 범위로 (별도 배포)

**Goal:** `PATCH /devices/{id}/settings` 의 `target_temp_min/max`, `target_humid_min/max` 를 v4 1-1·1-2 범위로 검증. 앱이 범위 밖 값을 보내지 않는다는 확인 후 배포.

**Files:** Modify `backend/routers/settings.py:45-95`, Test `tests/test_settings_api.py`, Modify `docs/API.md`

**Must know:**
- 온도: 요청에 들어온 필드만 15.0 이상 29.0 이하, 0.5 의 배수. 습도: 35 이상 85 이하, 5 의 배수. MIN < MAX 는 **병합 결과**로 검사(같으면 400). 요청에 없는 기존 저장값의 범위는 검사하지 않음(사용자가 고칠 수 있게). `target_temp_c`, `target_humidity_pct` 는 기존 범위 유지. 400 detail 은 한국어.

- [ ] **Step 1: 실패 테스트**

```python
def test_patch_temp_minmax_v4_range():           # max=29.5 → 400, 29.0 → 200, min=14.5 → 400, 15.0 → 200
def test_patch_temp_step_half():                 # min=18.3 → 400, 18.5 → 200
def test_patch_humid_range_step():               # min=33 → 400, 37 → 400, 35 → 200, max=85 → 200, 90 → 400
def test_patch_min_equal_or_over_max_400():      # 병합 결과 min=max=24.0 → 400, min 25 / 저장 max 24 → 400
def test_patch_other_field_with_legacy_value():  # 저장값 max=35(구 범위) 상태에서 min=20 만 PATCH → 200
def test_patch_null_resets_to_default():         # min=None → 200 (판정은 디폴트 사용)
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: PASS** → **Step 5: 커밋** `"settings: 목표 온습도 MIN/MAX 검증을 v4 범위로(15~29℃ 0.5단위, 35~85% 5단위, MIN<MAX)"`
