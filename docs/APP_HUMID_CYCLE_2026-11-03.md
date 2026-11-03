# 앱 전달 — v4 습기 사이클 (환기 지연·분무 공백 알림, `humid_cycles`)

> **대상**: 앱 담당
> **작성**: terra-server 백엔드 담당, 2026-11-03
> **성격**: 기획 v4 0-2·1-2·1-3·1-6 의 습기 사이클(습기 이벤트·건기·환기 지연·15시간 분무 공백)을 서버가 판정합니다. 실시간 알림 2종은 기존 v4 알림과 같은 `alerts` 행 + `env.alert.triggered` 푸시로, 일일·주간 카드는 기존 카드 행 안의 문구로 나갑니다. 서버 스위치 `HUMID_CYCLES_ENABLED` 가 **꺼진 채 배포**됩니다.
> **관련**: [APP_ENV_ALERT_PUSH_2026-11-02.md](APP_ENV_ALERT_PUSH_2026-11-02.md)(v4 알림·푸시 공통), [APP_DAILY_ANALYSIS_2026-11-02.md](APP_DAILY_ANALYSIS_2026-11-02.md), [APP_WEEKLY_ANALYSIS_2026-11-02.md](APP_WEEKLY_ANALYSIS_2026-11-02.md), 서버 계획 `specs/stage-o-humid-cycle-v4.md`
> **답하는 것**: 앱 핸드오프 §8 의 가정 5개(아래 §1 표) — 앱 0.168.0 가정이 **전부 맞습니다.**

## 0. 요약

| # | 서버가 하는 일 | 앱이 할 일 |
|---|---|---|
| ① | `alerts` 에 `env_vent_delay`(환기 지연)·`env_mist_gap`(분무 공백) 행이 생김 | 앱 0.168.0 처리 그대로 — 실시간 카드 습기 사이클 줄 |
| ② | 두 kind 도 `env.alert.triggered` 푸시(시작 1회, 추가 푸시 없음) | 수신 끄기 `env_humid` 분기 그대로 |
| ③ | 일일·주간 습도 카드에 사이클 문구(`HUM_NORMAL`·`HUM_NO_MIST`·`HUM_DELAY`) | 서버 문구 그대로 표시 — 앱 변경 없음 |
| ④ | `humid_cycles` 테이블(습기 이벤트 1개 = 1행, 본인 SELECT) | **안 써도 됩니다.** 칩·카드는 위 ①③으로 충분. 디버그·향후 화면용 |

## 1. 앱 §8 가정 확인

| # | 앱 가정 | 서버 확정 |
|---|---|---|
| 1 | kind `env_vent_delay`, `env_mist_gap` | 맞음 |
| 2 | `context.rule = 'v4'`, `message = "[칩] 제목"` | 맞음 — `[환기 지연] 분무 후 건기 미도달`, `[건조 주의] 분무 필요` (기획 1-5) |
| 3 | `context.level` | 두 kind 모두 **`caution`**(severity `warning`) |
| 4 | 푸시 payload `kind` 가 위 이름 | 맞음. `phase` 는 `started` 만(1시간 추가 푸시 `reminder` 없음), `threshold` 는 `null`, `value` 는 그 시점 습도 |
| 5 | 수신 끄기는 계정 `env_humid` | 서버는 설정과 무관하게 적재 — 앱 분기 그대로. **분무 공백도 푸시합니다** |

## 2. 판정 규칙 (앱 표시에 참고)

- **습기 이벤트**: 30초 이동 중앙값 습도가 지난 3분 최저값보다 **+10%p**(최저값 60% 미만) 또는 **+5%p**(최저값 60% 이상) 오르고 60% 이상이면. 기기 분무 명령은 쓰지 않고 습도로만 봅니다(펌프 막힘·물통 빔 대비). 시작 후 20분 안에 다시 오르면 같은 이벤트.
- **MAX** = 감지 후 3분 최고 습도, 최대 85%. **건기 목표** = MAX − 12%p.
- **환기 지연**(`env_vent_delay`): 시작 후 6시간까지 건기 목표에 못 닿으면 생성·푸시. 닿으면 종료(`resolved_at` = 도달 시각). 15시간이 지나도 못 닿으면 조용히 종료(`context.closed_reason = "stale"`).
- **분무 공백**(`env_mist_gap`): 최근 습기 이벤트가 **건기에 닿은 뒤**, 그 이벤트 시작부터 15시간 동안 새 습기 이벤트가 없으면 생성·푸시. 아직 안 마른 사육장엔 보내지 않습니다. 새 습기 이벤트가 잡히면 종료.
- 6시간·15시간에서 센서 연결이 끊긴 시간은 뺍니다(그만큼 늦게 울림). 스위치를 켠 뒤 습기 이벤트가 한 번도 없던 기기는 분무 공백이 나가지 않습니다.
- `context` 키: `rule`, `level`, `cycle_id`(humid_cycles.id), `cycle_started_at`(습기 이벤트 시작), `value`, `max_humid`, `dry_target`, `pushed_at`, 종료 시 `closed_reason`(`dry`/`next_event`/`stale`/`unlinked`/`disabled`).

## 3. 일일·주간 카드 변화 (스위치 켠 뒤)

| 카드 | 변화 |
|---|---|
| 일일 습도, 이벤트 없는 날 | `HUM_NORMAL`(건습 사이클이 건강하게 유지됐어요 / 습기 N번, 건기 M번) 또는 `HUM_NO_MIST`(습기가 감지되지 않았어요 / 분무를 잊지 않으셨는지 확인해 주세요). 습기 이벤트는 있는데 건기 도달 0이면 카드 없음 |
| 일일 습도, 환기 지연 | `HUM_DELAY` 줄 `{시작}에 분무 후 사육장이 충분히 마르지 않음` (Stage P 그대로) |
| 주간 습도 | `HUM_DELAY`(환기 지연 2회 이상), `HUM_NORMAL`(`{dry_time}`), **새 `HUM_NO_MIST`**(그 주 습도 이벤트 0·습기 이벤트 0, 일일과 같은 문구) |

- 습기 사이클의 하루는 **05:00~다음 날 05:00**(기획 확정). 카드 구간(08:00~08:00)과 3시간 다릅니다 — 새벽 2~5시에 시작한 습기 이벤트는 전날 카드에, 그 환기 지연(6시간 뒤 08~11시)은 다음 날 카드에 나올 수 있습니다.
- 건기 도달 수는 카드 구간 끝(08:00)까지 닿은 것만 셉니다.
- 습도가 비는 날(유효 수집일 미달)과 스위치를 켠 날·주는 사이클 문구를 만들지 않습니다(카드 없음).

## 4. `humid_cycles` (선택)

| 컬럼 | 의미 |
|---|---|
| `started_at` / `start_humid` | 습기 이벤트 시작(급상승 직전 최저점) |
| `max_humid` / `max_raw` | MAX(상한 85) / 상한 전 최고값 |
| `dry_target` / `dry_reached_at` | 건기 목표 / 최초 도달 시각 |
| `status` | `open` / `reached_in_time` / `reached_late` / `not_reached` |
| `offline_sec` | 판정에서 뺀 센서 공백(초) |
| `vent_delay_alert_id` / `mist_gap_alert_id` | 연결된 alerts 행 |

RLS 본인 기기 SELECT 만, Realtime 없음.

## 5. 켜는 순서

1. (완료) `humid_cycles` 운영 마이그레이션 — 2026-11-03.
2. 서버 PR 머지·배포 — 스위치 꺼짐이라 변화 없음.
3. 1단계(`ALERTS_V4_ENABLED`)가 켜진 뒤 `HUMID_CYCLES_ENABLED=true` + `HUMID_CYCLES_SINCE=<켠 시각>` → 두 kind 행이 생기기 시작(푸시는 2단계 `PUSH_ENV_ALERTS_ENABLED` 와 같이).
4. 켜면 시각을 알려 드릴게요. 실기기 칩 확인 부탁드립니다.
