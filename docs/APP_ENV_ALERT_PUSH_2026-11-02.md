# 앱 전달 — v4 실시간 온습도 알림(`env_*` alerts) + `env.alert.triggered` 푸시

> **대상**: 앱 개발 담당
> **작성**: terra-server 백엔드 담당, 2026-11-02
> **성격**: 기획 v4 1-1~1-6(실시간 판정·푸시)을 서버가 판정해 `alerts` 에 **새 kind 4개**로 기록하고, 푸시용 이벤트 `env.alert.triggered` 를 기존 `push_outbox` → 앱 Edge Function 경로로 보냅니다. 서버 스위치 2개가 **모두 꺼진 상태로 배포**되어, 앱 회신 전에는 아무것도 바뀌지 않습니다.
> **관련 커밋**: 서버 `9aa5ec0`(판정) · `7502641`(푸시 이벤트) · `50c3b3b`(alerts 연결). 설계: [specs/stage-n-env-alerts-v4.md](../specs/stage-n-env-alerts-v4.md)

---

## 0. 요약

| # | 항목 | 앱에서 할 일 | 권장도 |
|:---:|---|---|:---:|
| ① | `alerts` 에 `env_temp_high` / `env_temp_low` / `env_humid_low` / `env_humid_high` 가 생김 (§1) | 실시간 상태 칩·알림 목록에 표시 | 필수 |
| ② | 새 푸시 이벤트 `env.alert.triggered` (`phase` = `started` / `reminder`) (§2) | Edge Function 수신부에 type 추가 | 필수 |
| ③ | 푸시 문구는 서버가 `payload.title` 로 완성해서 보냄 (§2.2) | 그대로 쓸지 결정 | 결정 |
| ④ | 사용자 "푸시 수신 OFF" 처리 (§3) | 서버는 항상 적재 — 거르기는 앱 서버에서 | 확인 |
| ⑤ | 켜는 순서 (§4) | 단계별 준비 완료 회신 | 필수 |

---

## 1. `alerts` 행 — 새 kind

기존 `temp_high` / `temp_low` / `humid_low`(구 판정·펌웨어)와 **이름이 다릅니다.** v4 판정이 켜지면 서버 판정 행은 아래 4개만 생기고, 펌웨어가 직접 보내는 `heater_latched`·`temp_high` 등은 지금처럼 남습니다.

| kind | 의미 | severity | 기획 이름 |
|---|---|---|---|
| `env_temp_high` | 사용자 MAX 초과 / 29℃ 초과 | 주의 `warning` / 경고 `critical` | 고온 주의 / 고온 경고 |
| `env_temp_low` | 사용자 MIN 미만 / 15℃ 미만 | 주의 `warning` / 경고 `critical` | 저온 주의 / 저온 경고 |
| `env_humid_low` | 사용자 MIN 미만 2분 | `warning` | 건조 주의 |
| `env_humid_high` | 사용자 MAX 초과 **2시간 연속** (2026-11-09 1시간 → 2시간) | `warning` | 과습 안내 (2026-11-09 과습 주의 → 과습 안내) |

- `message` = 기획 1-5 타이틀 그대로 (예: `[고온 주의] 적정 온도보다 +0.3℃`).
- `triggered_at` = 실제로 범위를 벗어나기 **시작한** 시각(2분 확정 시각이 아님). `resolved_at` = 적정 범위로 돌아온 시각(5분 유지가 확인된 뒤 기록되지만 값은 돌아온 시각).
- 주의 → 경고: 주의 행이 끝나고(`resolved_at`) 경고 행이 새로 생깁니다. 두 시각이 같습니다.
- `context` (jsonb):

| 키 | 설명 |
|---|---|
| `rule` | 항상 `"v4"` |
| `level` | `caution` / `danger` |
| `value` | 이벤트 확정 시 측정값 |
| `threshold` | 넘은 쪽 **사용자** MIN/MAX (경고여도 사용자 값) |
| `diff` | `|value − threshold|`, 온도 0.1℃·습도 1%p 단위 **올림** |
| `peak_value` / `peak_diff` | 이벤트 중 최대 이탈값과 그 차이. 진행 중에도 5분마다 갱신, 종료 시 확정 |
| `pushed_at` / `reminded_at` | 푸시·추가 푸시를 outbox 에 넣은 시각(넣었을 때만) |
| `deescalated_from` | 경고가 나아져 **조용히** 주의로 바뀐 행이면 이전 경고 id (이 행은 푸시 없음) |
| `push_suppressed` | 위 무음 주의에서 30분 안에 다시 경고가 된 행이면 `true` (푸시 없음) |
| `chain_root` | 위 두 경우, 처음 경고 행 id — 분석에서 한 건으로 묶는 키 |
| `chain_pushed_at` / `chain_reminded` | 위 두 경우, 처음 경고의 푸시 시각·추가 푸시 여부 (서버 내부용 — 재경고의 1시간 추가 푸시 기준) |

> 실시간 그래프 상태 칩은 `message` 의 `[ ]` 안 문구(기획 1-5)로 표시하면 됩니다.

> **2026-11-03 추가(Stage O)**: 습기 사이클 kind 2개 `env_vent_delay`(`[환기 지연] 분무 후 건기 미도달`)·`env_mist_gap`(`[건조 주의] 분무 필요`)도 같은 형식(`rule = "v4"`, `level = "caution"`, severity `warning`)과 같은 푸시로 나갑니다. 푸시는 시작 1회뿐(추가 푸시 없음), `threshold = null`. 상세: [APP_HUMID_CYCLE_2026-11-03.md](APP_HUMID_CYCLE_2026-11-03.md)

## 2. 푸시 이벤트 `env.alert.triggered`

봉투는 기존 `device.action.*` 와 같습니다([BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md](BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md)).

### 2.1 예시

```json
{
  "schema_version": 1,
  "event_id": "alert:5ec3b4d1-...:started",
  "type": "env.alert.triggered",
  "occurred_at": "2026-11-02T05:02:03+00:00",
  "user_id": "<devices.owner_id>",
  "payload": {
    "alert_id": "5ec3b4d1-...",
    "device_id": "<devices.id>",
    "device_key": "terra-1a2b3c4d",
    "device_name": "거실 사육장",
    "enclosure_id": "<enclosures.id>",
    "kind": "env_temp_high",
    "level": "caution",
    "phase": "started",
    "title": "[고온 주의] 적정 온도보다 +0.3℃",
    "value": 26.3,
    "threshold": 26.0
  }
}
```

- 1시간 뒤 추가 푸시는 같은 모양에 `phase: "reminder"`, `event_id: "alert:<id>:reminder"` 입니다. 이벤트당 최대 1회, **그 시점에 아직 범위 밖일 때만** 보냅니다.
- `event_id` 는 멱등 키입니다. 같은 값이 다시 와도 한 번만 보내 주세요.
- `device_key` 는 비어 있으면 **키 자체가 빠집니다**(기존 이벤트와 같은 규칙).
- `level` 은 `caution`/`danger` 입니다(`alerts.severity` 의 warning/critical 이 아님).

### 2.2 문구

`payload.title` 은 기획 1-5 타이틀 완성본입니다. 기획상 "푸시는 타이틀 문구만" 이라 서버가 만들어 보냅니다. 앱이 직접 조합하려면 `kind`·`level`·`value`·`threshold` 로 만들 수 있습니다 — 어느 쪽으로 할지 회신 부탁드립니다.

> **2026-11-09 변경**: `env_humid_high` 타이틀은 `[과습 안내] 적정 습도보다 +{diff}%p` 입니다(`alerts.message`·`payload.title` 공통, kind·level·severity 는 그대로). 서버는 타이틀만 보내므로, 그 아래 안내 문구는 앱에서 `최고 습도를 초과한 상태가 지속되고 있어요. 환기 상태를 점검해 주세요` 로 바꿔 주세요.

### 2.3 푸시가 가지 않는 경우

- 경고가 나아져 주의로 바뀔 때 (`deescalated_from`)
- 그 주의에서 30분 안에 다시 경고가 될 때 (`push_suppressed`). 단 처음 경고 푸시 1시간 뒤에도 위험 온도면 이 행으로 추가 푸시(`reminder`) 1회 — 체인 전체에서 1회
- 주의 2분 확정 시점에 경고 조건이 이미 진행 중일 때 (주의를 건너뛰고 경고만)
- 종료(회복) — 기획 1-4 대로 발송 안 함

## 3. 사용자 푸시 수신 OFF

기획 1-5: "푸시 수신 설정이 OFF 인 경우 푸시는 발송하지 않지만 데이터 수집 및 분석 이벤트는 정상적으로 기록한다."
서버는 수신 설정을 모르므로 **항상 적재**합니다. 수신 OFF 사용자 거르기는 앱 Edge Function 에서 해 주세요. 설정이 서버 DB 에 있어야 한다면 알려 주세요.

## 4. 켜는 순서

| 단계 | 서버 | 앱에서 보이는 변화 | 필요한 회신 |
|---|---|---|---|
| 0 | 배포 (두 스위치 꺼짐) | 없음 (구 판정 그대로) | — |
| 1 | `ALERTS_V4_ENABLED=true` | Realtime 으로 `env_*` 행이 들어옴. 구 판정 `temp_high`/`temp_low`/`humid_low` 는 더 안 생김(진행 중이던 구 행은 종료됨). 푸시 없음 | §1 표시 준비 완료 |
| 2 | `PUSH_ENV_ALERTS_ENABLED=true` | `env.alert.triggered` 수신 시작 | §2 수신부 준비 완료 |

롤백: 서버가 스위치를 끄면 1단계 이전으로 돌아가고, 진행 중 `env_*` 행은 서버가 일괄 종료합니다.
