# 앱 전달 — v4 일일 온습도 분석 카드 (`env_daily_reports`)

> **대상**: 앱 개발 담당
> **작성**: terra-server 백엔드 담당, 2026-11-02
> **성격**: 기획 v4 2-1~2-3(일일 분석)을 서버가 매일 아침 기기별로 만들어 `env_daily_reports` 에 저장합니다. 문구는 서버가 기획 원문대로 완성해 저장하니 앱은 **그대로 그리면** 됩니다. 앱은 RLS 로 본인 기기 행만 읽고, Realtime 으로 새 리포트를 받을 수 있습니다.
> **선행**: v4 실시간 알림([APP_ENV_ALERT_PUSH_2026-11-02.md](APP_ENV_ALERT_PUSH_2026-11-02.md)) — 일일 분석은 그 `env_*` alerts 를 요약합니다. 서버 스위치 `ALERTS_V4_ENABLED` 와 `ENV_REPORTS_ENABLED` 가 **둘 다 켜져야** 생성됩니다(기본 꺼짐). 실시간 판정을 켠 날 이후 08:00 에 시작하는 날부터 만들어집니다(켜기 전 구간을 "이벤트 없음"으로 보여 주지 않게).
> 설계: [specs/stage-p-daily-analysis-v4.md](../specs/stage-p-daily-analysis-v4.md)

---

## 0. 요약

| # | 항목 | 앱에서 할 일 | 권장도 |
|:---:|---|---|:---:|
| ① | `env_daily_reports` 행 = 기기 × 날짜 1행, 카드 2장(`temp_card`, `humid_card`) (§1) | 분석 화면에서 조회 | 필수 |
| ② | 카드 JSON 은 `title` + `lines` 를 그대로 표시 (§2) | 렌더링 | 필수 |
| ③ | 09:05 이후 내용이 한 번 바뀔 수 있음(`pass` 1→2) (§3) | Realtime UPDATE 도 받기 | 권장 |
| ④ | 카드가 `null` 인 날이 있음 (§4) | 빈 상태 처리 | 필수 |
| ⑤ | 카드 `status` 는 `danger`/`caution`/`normal` — alerts `severity` 와 어휘가 다름 (§2.2) | 색·아이콘 매핑 | 필수 |

---

## 1. 테이블

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid | PK |
| `device_id` | uuid | `devices.id` |
| `report_date` | date | **집계 시작일(KST)**. `2026-11-02` = 11/2 08:00 ~ 11/3 08:00 |
| `window_start` / `window_end` | timestamptz | 위 구간(UTC 저장) |
| `temp_card` | jsonb \| null | 온도 카드 (§2) |
| `humid_card` | jsonb \| null | 습도 카드 (§2) |
| `pass` | smallint | 1 = 08시대 1차 생성, 2 = 09:05 이후 재계산에서 내용이 바뀌어 다시 씀. 1 로 남아 있으면 재계산 결과가 같았다는 뜻(최종) |
| `generated_at` | timestamptz | 마지막 생성 시각 |

- UNIQUE(`device_id`, `report_date`). 앱 화면의 "어제 분석" = 오늘 아침 생성된 `report_date = 어제` 행입니다.
- RLS: 본인 기기 행 SELECT 만. 쓰기는 서버만.

```dart
final rows = await sb.from('env_daily_reports')
  .select()
  .eq('device_id', deviceId)
  .order('report_date', ascending: false)
  .limit(7);

sb.channel('daily-reports-rt')
  .onPostgresChanges(
    event: PostgresChangeEvent.all,      // INSERT(1차) + UPDATE(2차)
    schema: 'public', table: 'env_daily_reports',
    callback: (payload) { /* payload.newRecord 로 카드 갱신 */ },
  ).subscribe();
```

## 2. 카드 JSON

### 2.1 예시

이벤트가 있는 날 (고온 주의 → 고온 경고 → 나아져 조용히 주의로 내려온 구간까지 한 줄):

```json
{
  "element": "temp",
  "code": "TEMP_DANGER_HIGH",
  "status": "danger",
  "title": "위험한 온도가 기록되었어요",
  "lines": [
    "14:00~14:30 적정 온도보다 최대 +1.8℃ 이탈",
    "14:30~16:20 위험 온도 29.6℃ 감지"
  ],
  "data": {
    "events": [
      {"root": "a1", "ids": ["a1"], "kind": "env_temp_high", "level": "caution",
       "start": "2026-11-02T05:00:00+00:00", "end": "2026-11-02T05:30:00+00:00"},
      {"root": "a2", "ids": ["a2", "a3"], "kind": "env_temp_high", "level": "danger",
       "start": "2026-11-02T05:30:00+00:00", "end": "2026-11-02T07:20:00+00:00"}
    ],
    "day_avg": 25.1, "night_avg": 21.8, "diff_daynight": 3.3, "valid_day": true
  }
}
```

이벤트가 없는 날 (주야간 온도차):

```json
{
  "element": "temp", "code": "TEMP_NORMAL", "status": "normal",
  "title": "주간과 야간 모두 적정 온도를 유지했어요",
  "lines": ["안정적인 온도 유지 중. 도마뱀이 살기 좋은 환경이에요"],
  "data": {"events": [], "day_avg": 25.1, "night_avg": 21.8, "diff_daynight": 3.3, "valid_day": true}
}
```

생성 시점에 아직 끝나지 않은 이벤트 (종료 시각 없이 `22:00~`):

```json
{
  "element": "humid", "code": "HUM_DRY", "status": "caution",
  "title": "건조를 주의해 주세요",
  "lines": ["22:00~ 적정 습도보다 최대 -9% 이탈"],
  "data": {"events": [{"root": "h1", "ids": ["h1"], "kind": "env_humid_low", "level": "caution",
                       "start": "2026-11-02T13:00:00+00:00", "end": null}]}
}
```

### 2.2 필드

| 필드 | 설명 |
|---|---|
| `element` | `temp` / `humid` |
| `code` | 카드 종류 (아래 표). 아이콘·색 분기용 |
| `status` | `danger`(온도 경고 또는 환기 지연 있음 — 주간 분석과 같은 기준) / `caution`(주의·주야간 이상) / `normal`. **alerts.severity 의 `warning`(=주의)·`critical`(=경고)과 다른 어휘**입니다 |
| `title` | 카드 타이틀 — 기획 2-2·2-3 원문 |
| `lines` | 세부 설명. 이벤트 카드는 이벤트 1건 = 1줄, 시작 시각순 |
| `data.events` | 줄과 같은 순서의 이벤트 묶음. `ids` 는 그 줄에 합쳐진 alerts id (상세 화면 연결용) |
| `data.day_avg` / `night_avg` / `diff_daynight` | 주간(08~20시)·야간(20~08시) 평균과 차이, 소수 1자리. 계산 못 하면 `null` |
| `data.valid_day` | 그날 온도 수집이 충분했는지 (하루 예정 28,800개의 80% 이상) |

| code | 타이틀 |
|---|---|
| `TEMP_DANGER_HIGH` / `TEMP_DANGER_LOW` | 위험한 온도가 기록되었어요 |
| `TEMP_CAUTION_HIGH` | 높은 온도가 기록됐어요 |
| `TEMP_CAUTION_LOW` | 낮은 온도가 기록됐어요 |
| `TEMP_DIFF_TOO_LOW` | 주간과 야간 온도가 거의 일정했어요 |
| `TEMP_NORMAL` | 주간과 야간 모두 적정 온도를 유지했어요 |
| `TEMP_DIFF_TOO_HIGH` | 주간과 야간의 온도차가 너무 커요 |
| `HUM_DRY` | 건조를 주의해 주세요 |
| `HUM_OVER` | 과습을 주의해 주세요 |
| `HUM_DELAY` | 건습 사이클이 건강하지 않아요 (습기 이벤트 확정 후) |
| `HUM_NORMAL` / `HUM_NO_MIST` | 건습 사이클이 건강하게 유지됐어요 / 습기가 감지되지 않았어요 (습기 이벤트 확정 후) |
| `ERR_DISCONNECT` | 데이터 수집이 원활하지 않았어요 — 온도·습도 공통, `status: "caution"`, `data: {"events": [], "valid_day": false}` (2026-11-12 추가, §4) |

### 2.3 시각 표기

- 시각은 KST `HH:MM`. **날짜 없는 시각은 `report_date` 그날**의 시각이고, 다른 날(다음 날 새벽 포함)의 시각에는 `M/D` 가 붙습니다. 범위 끝 시각은 시작과 같은 날이면 날짜를 생략합니다(2026-11-03 변경, 앱 요청). `report_date = 11/2` 기준 예: `14:00~15:00`, `11/3 02:00~03:00`, `23:30~11/3 00:40`, `22:00~11/3 08:20`, `11/1 23:10~09:30`, `07:50~08:20`(11/2 아침 — 앞 구간 끝에 걸친 이벤트).
- 경고가 나아져 조용히 주의로 내려온 구간은 원래 경고와 한 줄로 묶입니다(실시간 알림 문서 §1 `chain_root`).

## 3. 생성 시점 — 1차·2차

| | 시각(KST) | 동작 |
|---|---|---|
| 1차 | 08:00 이후 (마지막 30분 집계가 확인되거나 08:15 지나면) | 행이 없을 때만 INSERT, `pass=1` |
| 2차 | 09:05 이후 1회 | 다시 계산해 **바뀌었을 때만** UPDATE, `pass=2` (같으면 쓰지 않음 — Realtime 이벤트 없음) |
| (서버가 08시대에 멈춰 있었을 때) | 09:05 이후 처음 생성 | 바로 `pass=2` 로 1회 INSERT |

2차가 필요한 이유: 07:59 에 시작한 이벤트는 2분 확정이 08:01 이라 1차에 빠질 수 있고, 08:20 에 끝난 이벤트는 1차엔 `22:00~` 로, 2차엔 `22:00~11/3 08:20` 으로 보입니다. **Realtime UPDATE 를 받거나 화면 진입 시 다시 조회**해 주세요.

## 4. 카드가 `null` 인 날

| 카드 | null 인 경우 |
|---|---|
| `temp_card` | 그날 온도 이벤트가 없고, 등록 첫날이라 수집이 부족했거나 주간·야간 중 한쪽 데이터가 없을 때 |
| `humid_card` | 그날 습도 이벤트(건조·과습·환기 지연)가 없고, 습기 사이클 스위치가 꺼져 있거나 켠 날이거나 등록 첫날이라 수집이 부족했을 때, 또는 습기 이벤트는 있었는데 08:00 까지 건기 도달이 0일 때. 스위치를 켠 뒤엔 `HUM_NORMAL`/`HUM_NO_MIST` 가 나옵니다(습기 사이클 하루 = 05:00~05:00, [APP_HUMID_CYCLE_2026-11-03.md](APP_HUMID_CYCLE_2026-11-03.md) §3) |

이벤트 카드는 수집 상태와 무관하게 항상 만듭니다.

### 4.1 수집 부족 안내 `ERR_DISCONNECT` (2026-11-12 추가, 앱 요청 · Figma `1736:5264`)

위 경우로 카드가 없고 **그 지표가 유효 수집일이 아니면**(하루 예정 28,800개의 80% 미만), 그 지표 카드 자리에 안내 카드를 넣습니다.

```json
"temp_card": {
  "element": "temp", "code": "ERR_DISCONNECT", "status": "caution",
  "title": "데이터 수집이 원활하지 않았어요",
  "lines": ["일일 분석을 위한 데이터가 충분하지 않아요. 전원과 Wi-Fi 연결 상태를 확인해 주세요"],
  "data": {"events": [], "valid_day": false}
}
```

- 지표별로 따로 봅니다. 온도·습도가 둘 다 모자라면 두 카드 모두 안내입니다.
- 그날 이벤트가 있으면 수집이 모자라도 **이벤트 카드만** 옵니다(주간 Q4 r2 와 같은 방향).
- 기기 등록 시각이 구간 시작(08:00)보다 늦은 날(등록 첫날)은 내지 않습니다 — 덜 모인 게 당연한 날이라 "Wi-Fi 확인" 오안내가 됩니다.
- 유효 수집일인데 카드가 없는 날(습도 스위치 꺼짐, 주간·야간 한쪽 비어 있음 등)은 안내 없이 `null` 그대로입니다.
- 주간 `notices` 의 `ERR_DISCONNECT`(유효일 3일 이상·누락률 50% 이상)와 **이름만 같고 기준·자리가 다릅니다.**
- 배포 이전 날짜의 행은 다시 만들지 않습니다(빈 칸 그대로).
