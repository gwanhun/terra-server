# 앱 전달 — 냉각팬(fan2) 기능 추가 (2026-09-07)

> **받는 쪽**: 앱(Flutter)
> **성격**: 두 번째 팬 = **냉각팬** 신규 노출. 펌웨어·서버·웹 구현/배포 완료 — **앱 UI만 남음**.
> **배포 상태**: `main` 반영(`3e07d04`) + 운영(`api.terra-server.uk`) 재시작 완료. 마이그레이션(`2026-09-07_telemetry_fan2.sql`) Supabase 적용 완료.

---

## 0. 한눈에

| # | 항목 | 엔드포인트/필드 | 앱측 할 일 |
|---|---|---|---|
| §1 | 즉시 제어 | `commands` INSERT — `fan2_on` / `fan2_off` / `fan2_toggle` | 기존 팬 버튼 로직 복제, 라벨만 "냉각팬" |
| §2 | 상태 표시 | `telemetry.fan2` (`'ON'`\|`'OFF'`\|`null`) | 상태 뱃지 추가. `null` 이면 컨트롤 숨김 |
| §3 | 예약 | `POST /devices/{id}/schedules` — `fan2_on` / `fan2_off` | 예약 action 목록에 추가 (구간/guard 동일) |
| §4 | 지원 판별 | `telemetry.fan2 != null` | capabilities 플래그 없음 — telemetry 로 판별 |

**용어 통일**: `fan` = **팬**, `fan2` = **냉각팬**. 기능·프로토콜은 두 팬이 완전히 동일하고 이름만 다르다.

---

## §1 — 즉시 제어 (commands INSERT)

기존 팬과 동일한 경로 — Supabase `commands` 테이블 직접 INSERT. REST 엔드포인트 아님 (REST 는 mist 만).

```jsonc
// supabase.from('commands').insert(...)
{
  "device_id": "<devices.id UUID>",
  "issued_by": "<auth user id>",
  "action": "fan2_on",          // fan2_on | fan2_off | fan2_toggle
  "payload": null                // 아래 duration_ms 참고
}
```

- **`fan2_on` + `payload: {"duration_ms": N}`** → N ms 후 펌웨어가 **자동 OFF** (one-shot 타이머).
  - 상한: 펌웨어가 **2시간(7,200,000ms)** 으로 clamp.
  - `duration_ms` 없으면 계속 ON.
  - 타이머 진행 중 `fan2_off` 를 보내면 타이머 취소 + 즉시 OFF.
- **`fan2_toggle`** — 즉시 제어에서만 사용 (예약은 §3 대로 절대 명령만).
- 상태 추적: 기존과 동일 — INSERT 후 `commands` Realtime 으로 `status: 'pending' → 'acked'` + `result` 구독.

## §2 — 상태 표시 (telemetry.fan2)

`telemetry` 에 **`fan2`** 컬럼 추가 (TEXT, nullable). 기존 `fan` 과 동일 형식.

```jsonc
// telemetry Realtime INSERT payload (관련 필드만)
{ "relay": "OFF", "fan": "ON", "fan2": "OFF", "led": "ON", "led_brightness": 75 }
```

- `'ON'` / `'OFF'` — 냉각팬 실제 상태. 로컬 플래그로 추측하지 말고 이 값 사용 (LED §4 와 동일 원칙).
- **`null`** — 펌웨어 미보고(구버전) 또는 아직 telemetry 없음 → 냉각팬 컨트롤 숨기거나 `-` 표시.

## §3 — 예약 (schedules)

`POST /devices/{id}/schedules` 허용 action 에 **`fan2_on`, `fan2_off`** 추가.

- 기존 정책 그대로: **toggle 계열은 예약 불가**(서버 400, 절대 명령만).
- 구간 예약(`pair_id` 로 on/off 2행 묶기), guard(`skip_when_*`/`stop_when_*`), `duration_ms` payload 모두 기존 팬과 동일하게 동작.
- 앱은 예약 생성 화면의 action 선택지에 "냉각팬 켜기/끄기"만 추가하면 됨.

## §4 — 기기 지원 판별

- `devices.capabilities` 에는 fan2 관련 플래그가 **없다** (led_dimmable 만 존재).
- 현재 운영 중인 펌웨어(terra-fw 0.1.0, nano/supermini 공통)는 전부 fan2 지원.
- 판별이 필요하면 **최신 telemetry 의 `fan2` 가 non-null 인지**로 확인하는 것을 권장.
- 하드웨어: MOSFET 4채널 보드의 **PWM4** 채널이 냉각팬 (PWM1=조명, PWM2=펌프, PWM3=팬).

---

## 참고

- 웹 레퍼런스 구현: `web/index.html` — 냉각팬 토글 버튼(❄️)·예약 옵션·텔레메트리 컬럼 (커밋 `3e07d04`).
- MQTT 계약: [docs/MQTT.md](MQTT.md) §1 telemetry `fan2`, §2 command `fan2_*`.
- 마이그레이션: `migrations/2026-09-07_telemetry_fan2.sql` (적용 완료 — [MIGRATIONS_APPLIED.md](../MIGRATIONS_APPLIED.md)).
