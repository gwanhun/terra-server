# 앱 전달 — v4 주간 온습도 분석 카드 (`env_weekly_reports`)

> **대상**: 앱 개발 담당
> **작성**: terra-server 백엔드 담당, 2026-11-02
> **성격**: 기획 v4 3-1~3-3(주간 분석)·4-1(예외)을 서버가 매주 기기별로 만들어 `env_weekly_reports` 에 저장합니다. 일일 분석([APP_DAILY_ANALYSIS_2026-11-02.md](APP_DAILY_ANALYSIS_2026-11-02.md))과 같은 카드 JSON 형식이고, 문구는 서버가 기획 원문대로 완성합니다. 앱은 RLS 로 본인 기기 행만 읽고 Realtime 으로 받을 수 있습니다.
> **켜짐 조건**: 일일 분석과 같은 서버 스위치(`ALERTS_V4_ENABLED`, `ENV_REPORTS_ENABLED`, `ALERTS_V4_SINCE`). 실시간 판정을 켠 뒤 **처음으로 온전히 시작한 주**부터 만들어집니다.
> 설계: [specs/stage-q-weekly-analysis-v4.md](../specs/stage-q-weekly-analysis-v4.md)

---

## 0. 요약

| # | 항목 | 앱에서 할 일 | 권장도 |
|:---:|---|---|:---:|
| ① | `env_weekly_reports` 행 = 기기 × 주 1행, 카드 2장 + `notices` (§1) | 주간 분석 화면에서 조회 | 필수 |
| ② | 카드는 대표 케이스 1개, `title` + `lines` 그대로 (§2) | 렌더링 | 필수 |
| ③ | `notices`: 데이터 부족(ERR_NO_DATA)·수집 불안정(ERR_DISCONNECT) 안내 (§3) | 카드 위 안내 표시 | 필수 |
| ④ | 카드가 `null` 인 경우 (§4) | 빈 상태 처리 | 필수 |

---

## 1. 테이블

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid | PK |
| `device_id` | uuid | `devices.id` |
| `week_start` | date | 집계 시작 **월요일(KST)**. `2026-11-02` = 11/2(월) 08:00 ~ 11/9(월) 08:00 |
| `window_start` / `window_end` | timestamptz | 위 구간(UTC 저장) |
| `temp_card` / `humid_card` | jsonb \| null | 주간 카드 (§2) |
| `notices` | jsonb | 예외 안내 목록 `[{code, metric, title, body}]`, 없으면 `[]` (§3) |
| `generated_at` | timestamptz | 생성 시각 |

- UNIQUE(`device_id`, `week_start`). 주당 한 번만 만들고 다시 쓰지 않습니다.
- 생성: **월요일 09:05 이후** 첫 처리(5분 주기). 구간은 월 08:00 에 끝나지만, 08시 전후에 늦게 기록되는 실시간 이벤트(예: 07:10 시작 과습은 1시간 확정이라 08:10 기록)를 담으려고 일일 분석 재계산과 같은 시각까지 기다립니다. 서버가 월요일에 멈춰 있었으면 **그 주 안에** 복구해서 만듭니다. 그보다 오래된 주는 만들지 않습니다.
- UI 표시 기간은 기획대로 월~일로 보여 주세요(구간은 월 08:00 ~ 다음 월 08:00).

```dart
final rows = await sb.from('env_weekly_reports')
  .select()
  .eq('device_id', deviceId)
  .order('week_start', ascending: false)
  .limit(4);
```

## 2. 카드 JSON

```json
{
  "element": "temp",
  "code": "TEMP_DANGER",
  "status": "danger",
  "title": "주간 중 위험 온도가 감지되었어요",
  "lines": ["총 2회 위험 온도 발생. 적정 온도보다 최대 4.1℃ 이탈"],
  "data": {
    "valid_days": 7, "missing_rate": 0.0,
    "n_danger": 2, "n_temp_caution": 0, "max_deviation": 4.1,
    "week_avg": 23.4, "prev_week_avg": null, "diff_prev": null, "diff_daynight": 3.2,
    "events": [
      {"root": "d1", "ids": ["d1"], "kind": "env_temp_high", "level": "danger",
       "start": "2026-11-03T05:10:00+00:00", "end": "2026-11-03T06:00:00+00:00"}
    ]
  }
}
```

```json
{
  "element": "humid", "code": "HUM_DRY", "status": "caution",
  "title": "사육장이 건조한 날이 많았어요",
  "lines": ["총 4회 건조 주의가 발생. 사육장에 수분을 보충해 주세요"],
  "data": {"valid_days": 7, "missing_rate": 0.0, "n_dry": 4, "n_over": 0, "n_delay": 0, "events": ["…"]}
}
```

- `lines` 순서는 기획의 카테고리 순서([1] 이벤트 요약 → [2] 전주 대비 → [3] 패턴 분석)이고, 데이터가 없는 줄은 빠집니다. 예: 위험 온도가 같은 시간대에 3일 이상 **시작**하지 않았으면 "…에 온도 이탈이 집중되었어요" 줄이 없습니다.
- 횟수는 경고가 나아졌다 다시 나빠진 묶음(`chain_root`)을 1회로 셉니다(실시간 알림 문서 §1). `data.events` 는 일일 카드와 같은 형식(`ids` 포함)입니다.
- 이탈 폭을 계산할 수 없는 드문 경우 TEMP_DANGER 첫 줄은 `총 N회 위험 온도 발생.` 까지만 나옵니다.
- `status`: `danger`(TEMP_DANGER, HUM_DELAY) / `caution`(그 외 이상 케이스) / `normal`(TEMP_NORMAL, HUM_NORMAL). 일일 카드와 같은 어휘입니다.

| code | status | 타이틀 |
|---|---|---|
| `TEMP_DANGER` | danger | 주간 중 위험 온도가 감지되었어요 |
| `TEMP_CAUTION` | caution | 적정 온도를 벗어난 경우가 많았어요 |
| `TEMP_DIFF_HIGH` | caution | 지난주보다 평균 온도가 높아졌어요 / 낮아졌어요 |
| `TEMP_DIFF_TOO_LOW` | caution | 주·야간 온도차가 거의 없었어요 |
| `TEMP_DIFF_TOO_HIGH` | caution | 주·야간 온도차가 컸어요 |
| `TEMP_NORMAL` | normal | 주·야간 온도차가 안정적으로 유지되었어요 |
| `HUM_DELAY` | danger | 사육장 환기가 지연되는 경향이 보여요 (습기 이벤트 확정 후) |
| `HUM_DRY` | caution | 사육장이 건조한 날이 많았어요 |
| `HUM_OVER` | caution | 사육장이 습한날이 많았어요 |
| `HUM_MIXED` | caution | 건조와 과습이 반복됐어요 |
| `HUM_UNSTABLE` | caution | 습도 이탈이 반복됐어요 |
| `HUM_NORMAL` | normal | 건습 사이클이 건강하게 유지되었어요 (습기 사이클 스위치 켠 뒤) |
| `HUM_NO_MIST` | caution | 습기가 감지되지 않았어요 / 분무를 잊지 않으셨는지 확인해 주세요 — 그 주 습도 이벤트 0·습기 이벤트 0 (2026-11-03 추가, 기획 3-3 "(대신 예외 멘트가 출력됨)" 자리에 일일 2-3 같은 상황 문구) |

## 3. `notices` — 예외 안내

```json
[{"code": "ERR_NO_DATA", "metric": "t_a",
  "title": "주간 분석을 위한 데이터를 모으고 있어요", "body": "최소 3일의 데이터가 필요해요"}]
```

| code | 언제 | 카드 |
|---|---|---|
| `ERR_NO_DATA` | 그 지표의 유효 수집일이 3일 미만 | 그 지표 카드는 `null` |
| `ERR_DISCONNECT` | 유효 수집일 3일 이상인데 누락률 50% 이상 | 그 지표에 주의·경고 이벤트가 있었으면 **카드만**(안내 없음), 없었으면 **카드 없이 안내만**. 이벤트가 있어도 카드가 안 만들어지면(예: 습도 이탈 1~2회) 안내만. 한 행에 카드와 같은 지표의 ERR_DISCONNECT 가 함께 오는 일은 없어요(2026-11-03 변경, 기획자 답) |

- `metric`: `t_a` = 온도 카드, `h_a` = 습도 카드. 해당 카드 영역(또는 그 자리)에 안내를 보여 주세요.
- 유효 수집일 = 그날(08:00~08:00) 수집이 예정의 80% 이상. 누락률은 주 중간에 등록한 기기면 등록 시점부터 셉니다.

## 4. 카드가 `null` 인 경우

| 카드 | null 인 경우 |
|---|---|
| `temp_card` | `ERR_NO_DATA`(t_a) 이거나, 이벤트·전주 대비가 없고 주야간 온도차를 계산할 유효일이 없을 때 |
| `humid_card` | `ERR_NO_DATA`(h_a) 이거나, 건조·과습 주의가 케이스 기준에 못 미칠 때. HUM_NORMAL·HUM_DELAY·HUM_NO_MIST 는 습기 사이클 스위치를 켠 뒤, 켠 주 다음 주부터(사이클 구간 월 05:00~) 나옵니다 |
