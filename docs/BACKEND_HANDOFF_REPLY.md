# 백엔드/펌웨어 → 앱 회신 — 물분무·타이머·일정 핸드오프 검토 (2026-08-12)

> 앱 개발자의 `backend-handoff-timer-mist.md` 검토 회신. 실제 펌웨어(terra-iot-nano) 코드 + 백엔드 코드 대조 결과.
> **결론: 지적 대부분 정확. 문서 오류(result/KST) 수정 완료. 요청 1은 이미 펌웨어에 있음 → 백엔드 화이트리스트만 추가.**

---

## 0. 한눈에

| 항목 | 판정 | 조치 |
|---|---|---|
| 요청 1 (heater/fan on/off) | ✅ **펌웨어에 이미 구현됨** | 백엔드 화이트리스트 추가만 (즉시 가능) |
| 요청 2 (스마트 조건 guard) | 신규 설계 필요 | 서버 vs 펌웨어 위치 결정 필요 |
| 요청 3 (일회성 타이머) | 부분 가능 | A안(duration_ms) 추천, fan은 쉬움/heater는 별도 |
| 요청 4 (LED 밝기 0~100%) | ✅ **구현됨** (MOSFET 보드), 실물 확인 필요 | 옵토절연 MOSFET → PWM 1kHz. 릴레이 보드는 on/off only |
| 요청 5 (commands 출처) | 타당 | source 컬럼 추가 (A안) |
| 질문 A (result 목록) | 앱 지적 맞음 | 문서 수정 완료 |
| 질문 B (relay_toggle) | 계속 유효 | 부무 매핑 유지 OK |
| 질문 C (issued_by) | = owner_id | 아래 답변 |

---

## 1. 질문 직접 답변 (A/B/C)

### A. `result` 값 전체 목록 — 앱 지적이 맞음, 문서 수정 완료

펌웨어(`command_dispatch.c`) 실제 어휘 **8종**:

| result | 발생 | 의미 |
|---|---|---|
| `ok` | 정상 | 실행됨 |
| `busy` | mist/spray | 이미 분무 중 → 무시 |
| `bad_request` | mist 등 | 필수 payload 누락 (duration_ms 등) |
| `error` | 모든 액추에이터 | GPIO/드라이버 구동 실패 (rc≠ESP_OK) |
| `locked` | **heater 계열만** | 히터 안전 latch 활성 → `heater_clear_lock` 먼저 |
| `unknown_action` | — | 펌웨어가 모르는 action |
| `expired` | — | TTL 만료 (SNTP 동기 시) |
| `duplicate` | — | 같은 msg_id 재전송 |

- 실DB의 `error`(15) `locked`(4)가 진짜인 이유 = 위 표. **앱 지적대로 내 문서에 빠졌었음 → `error` 추가, `rejected_unknown_action` 오타 수정 완료.**
- `locked` ↔ `APP_INTEGRATION.md`의 `rejected_locked` 대응 **맞음**. 둘은 서로 다른 펌웨어(nano vs P4 카메라)라 문자열이 다른 것. 앱은 **nano 기준(`locked`)** 으로 히터 잠금 안내하면 됨.
- `busy`/`bad_request`가 실DB에 0인 건 정상 — mist가 최근(08-11)에야 도입됐고 아직 그 경로를 안 밟아서.

### B. `relay_toggle` 향후 — **계속 유효**

펌웨어에 `relay_toggle` / `relay_on` / `relay_off` **다 있음** (워터펌프 릴레이). `mist` 는 그 펌프의 *타이머 버전*일 뿐, `relay_toggle` 을 대체/폐기하지 않음.
- 과거 144건(`relay_toggle`, 전부 ok)은 그대로 "부무 수동 토글"로 해석 유효.
- **앱이 `relay_toggle`·`mist` 둘 다 부무로 매핑해도 정확함.** 수동 on/off 토글은 `relay_toggle`, 정량 분무는 `mist`.

### C. 예약 발행 시 `issued_by` — **owner_id (수동과 동일)**

`schedule_runner.py`: 예약이 발행하는 `commands.issued_by = 예약 소유자(owner_id)`. 즉 **지금은 수동/예약/가드가 `issued_by`로 구분 안 됨** → 요청 5(출처 컬럼)가 타당한 이유. 가드까지 가면 `issued_by`만으로는 부족.

---

## 2. 요청별 검토

### 요청 1 — heater/fan on/off → ✅ 펌웨어에 이미 있음 (최우선, 즉시 가능)

**핵심: `heater_on` `heater_off` `fan_on` `fan_off` `relay_on` `relay_off` 전부 펌웨어에 구현돼 있음.**
앱 개발자가 몰랐던 이유 = `APP_INTEGRATION.md §3.2` 액션 표에 toggle 계열만 있고 on/off가 누락됐던 것 (문서 갭, 계약 부재 아님).

- 멱등성: 펌웨어가 `relay_on`/`relay_off`를 절대 상태로 처리 → 이미 켜져 있어도 `heater_on` 재전송 무해. ✅ 요구 충족.
- **막힌 지점은 백엔드 하나**: `schedules` 화이트리스트가 `{mist, fan_toggle, led_on, led_off, relay_toggle, heater_toggle}` 뿐 → `heater_on/off`, `fan_on/off`, `relay_on/off` 넣으면 예약에서도 사용 가능. (수동 명령은 `commands` 직접 INSERT라 지금도 됨.)
- **구간 예약**: on/off 2건(20:00 heater_on / 23:00 heater_off)으로 바로 가능. toggle의 상태 어긋남 위험 없음(절대 명령). 대안(`schedules.end_time_of_day`로 서버가 종료까지 챙김)도 UX상 좋지만, 급하면 2건 방식으로 즉시 착수 가능.

→ **조치**: 백엔드 화이트리스트 확장 + `APP_INTEGRATION.md §3.2`에 on/off 계열 문서화. 펌웨어 작업 0.

### 요청 2 — 스마트 조건(guard) → 신규, 위치 결정 필요

`schedules`에 조건 필드 없음(사실). 두 유형으로 갈림:
- **발행 직전 스킵** (`skip_when_humidity_above`): schedule_runner가 발행 직전 **최신 telemetry 조회 → 조건 맞으면 skip**. **서버 단독으로 쉽게 구현 가능.**
- **가동 중 정지** (`stop_when_temp_above`/`stop_when_humidity_below`): "켜고 나서 계속 감시하다 조건 되면 OFF"는 **지속 감시 주체 필요**. 앱은 죽으니 안 됨. 서버 폴링(수초~수십초 지연)도 가능하지만, **히터는 펌웨어 자체 안전 latch가 이미 있어**(과열 시 자동 차단), 펌웨어 조건 정지가 가장 견고. → 히터/팬 정지 조건은 펌웨어에 얹는 걸 권장.
- 가드 작동 시 `commands` 기록 = 요청 5(reason 컬럼)와 묶임.

→ **결정 필요**: guard 평가를 서버(schedule_runner)로 할지, 정지형은 펌웨어로 내릴지. skip형만 먼저(서버) 하고 stop형은 펌웨어 단계로 나누는 phasing 추천.

### 요청 3 — 일회성 타이머 → A안 추천, 대상별 난이도 다름

- **A안 (`duration_ms` 확장, 추천)**: `mist`와 계약 통일. `fan_on`+`{duration_ms}` → 자동 OFF.
  - **fan**: `s_fan`이 relay_handle → 펌웨어에 `relay_pulse` 이미 있음. **fan 타이머는 쉬움.**
  - **heater**: `heater_handle`(safety latch 별도 타입) → pulse 미구현. **별도 작업 필요.**
  - 남은시간 표시: 앱이 "발행시각+duration"으로 계산 가능(telemetry 불필요). 정밀 표시 원하면 telemetry에 remaining 추가.
  - 취소: **진행 중 pulse 중단 명령 필요** (예: `fan_off`가 pulse도 취소하도록 — relay 드라이버는 이미 "수동 off 시 auto-off 예약 취소" 구현됨. 확인함).
- **B안 (`device_timers` 테이블)**: 앱이 이미 스키마 가정(`actuator, duration_minutes, ends_at`). 취소/다기기 동기화 자연스러움. 단 새 테이블+만료 OFF 발행 로직.

→ **결정 필요**: A(계약 통일, 가벼움) vs B(앱 기존 가정과 일치). 개인적으론 **A + fan 먼저**, heater는 펌웨어 pulse 추가 후.

### 요청 4 — LED 밝기 0~100% → ✅ 구현됨 (MOSFET 보드), 실물 dim 확인 필요

> **⚠️ 정정 이력**: ① 처음엔 "하드웨어 미지원"이라 했다가 → ② MOSFET 보드라 PWM 가능으로 정정 →
> ③ 실물 모듈 사진 확인 결과 **옵토커플러 절연 MOSFET 모듈**(60N03 + PC817급 옵토). 아래가 최종.

**하드웨어 (실물 확인)**: 4채널 **옵토절연 N-MOSFET 모듈** (60N03). 신호 경로 = `GPIO → 옵토커플러 → MOSFET → LED`.
릴레이 아님 → **PWM 밝기 조절 물리적으로 가능.**

**구현 완료 (펌웨어 `terra-iot-nano`)**:
- `light_pwm` 모듈 신규 (LEDC, LCD 백라이트와 겹치지 않게 TIMER_1/CH_1)
- LED를 relay→PWM 전환, `led_on` 이 `payload.brightness`(0~100)를 duty로. `led_off`=0, `led_toggle` 유지
- **PWM 주파수 1kHz** — 입력이 옵토커플러(turn-off ~18μs)라 5kHz면 낮은 밝기에서 비선형. 1kHz면 0~100% 정확, 깜빡임 없음

**⚠️ 보드별 차이 (중요)**:
- **MOSFET 보드(`terra-iot-nano`)**: 밝기 PWM 적용됨.
- **릴레이 보드(`terra-iot-nano-relay`)**: 진짜 기계식 릴레이 → **밝기 불가**(PWM 하면 접점 손상). on/off 유지. 이 PWM 펌웨어를 릴레이 보드에 넣으면 안 됨.
- 앱: 밝기 슬라이더는 MOSFET 보드에서만 의미. 릴레이 보드는 `brightness` 보내도 on/off 로 처리(무시).

**남은 것**: `idf.py build flash` 후 **실물에서 `brightness:30` → 실제 어두워지는지 확인.** dim 이 거칠면 옵토가 더 느린 것 → 500Hz 로 조정.

> 참고: pump/fan 도 이 모듈로 구동되지만 mist/fan 타이머는 on/off(full duty)라 옵토 속도 무관. **밝기 PWM 만** 옵토 속도를 탐.

### 요청 5 — commands 출처 구분 → 타당, A안 추천

- 현재 `commands`에 출처 없음(사실). `issued_by`만으론 수동/예약/타이머/가드 구분 불가(답변 C).
- **A안 추천**: `source(manual|schedule|timer|guard)`, `source_id uuid`, `reason text` 컬럼 추가.
  - schedule_runner가 발행 시 `source='schedule', source_id=schedules.id`.
  - guard 정지 시 `source='guard', reason='목표 습도 50% 도달'` → PRD §4.3.8 감사 로그 충족.
- 마이그레이션 1건 + schedule_runner/guard 발행부 수정.

→ **결정 필요**: 컬럼 스키마 확정하면 마이그레이션 착수.

---

## 2.5 결정 필요 사항 — 상세 설명 (의사결정용)

> 아래 4개는 "이렇게 하면 이런 대가/이득"을 몰라 못 정하는 것들. 각 옵션의 **작업량·안전성·앱 영향**을 풀어 씀.
> 공통 원칙 하나: **안전 직결(히터 과열, 펌프 침수) 동작은 "서버가 나중에 꺼주는" 방식에 맡기지 않는다.**
> 서버/네트워크가 죽어도 펌웨어가 스스로 끄는 구조여야 함 (mist를 펌웨어 타이머로 만든 이유와 동일).

### 결정 ① — 스마트 조건(guard)을 어디서 판단할까 (요청 2)

**무엇을 정하나**: "조건이 맞으면 동작을 스킵/정지"를 서버가 볼지, 펌웨어가 볼지.

먼저 조건이 **두 종류**로 갈린다:
- **스킵형** — 발행 *순간* 1회 판단. 예: "분무 예약 시각인데 이미 습도 70% → 이번 회차 건너뜀". 판단 후 끝.
- **정지형** — 켜진 *내내* 감시. 예: "히터 켜고, 목표온도 도달하면 끈다". 계속 지켜봐야 함.

| 옵션 | 방식 | 장점 | 단점/위험 | 작업량 |
|---|---|---|---|---|
| **A. 전부 서버** | schedule_runner가 스킵형은 발행 직전 telemetry 조회로 판단, 정지형은 수초~수십초 주기 폴링하다 OFF 발행 | 펌웨어 수정 0, 조건 로직 한 곳, 조건값 바꾸기 쉬움 | 정지형이 **폴링 지연(수초~수십초)** → 과열 반응 굼뜸. **서버/브리지 죽으면 정지 안 됨(안전 취약)** | 중 |
| **B. 스킵형=서버, 정지형=펌웨어** | 스킵형은 A처럼. 정지형은 펌웨어가 "이 온도 넘으면 정지" 자체 latch (히터는 이미 안전 latch 존재) | **즉각 반응 + 서버 죽어도 안전.** 정지형에 정석 | 펌웨어 작업 + 조건값을 펌웨어로 전달·저장하는 계약 필요 | 큼 |

**추천: 단계 분리.** 1단계 = 스킵형만 서버로(빠르고 안전하며 대부분 유스케이스 커버). 2단계 = 정지형(특히 히터)은 펌웨어로. **히터 과열 정지를 서버 폴링에 맡기지 말 것.**
**이 결정이 푸는 것**: 예약이 "무조건 실행" → "조건부 실행(안전 가드)". PRD가 "이 기능의 핵심"이라 한 부분.

### 결정 ② — 일회성 타이머 방식: A(duration_ms) vs B(device_timers) (요청 3)

**무엇을 정하나**: "팬/히터 30분 켜고 자동 OFF, 취소 가능"을 어떤 구조로 만들지.

| 옵션 | 방식 | 장점 | 단점/위험 | 작업량 |
|---|---|---|---|---|
| **A. `duration_ms` 확장** | `fan_on {duration_ms:1800000}` → 펌웨어가 30분 뒤 **스스로** OFF. mist와 동일 계약 | **안전(서버 죽어도 펌웨어가 끔)**, 새 테이블 없음, mist와 통일 | 앱이 이미 `device_timers` 테이블 가정하고 UI 짬 → **앱 코드 수정** 필요. heater는 별도 작업 | fan=소, heater=중 |
| **B. `device_timers` 테이블** | 서버가 테이블 관리, 만료 시 OFF 발행, 취소=row 삭제 | **앱 기존 코드와 일치(앱 수정 최소)**, 취소·다기기 동기화·진행중 목록 자연스러움 | 새 테이블+만료 감시 러너. **OFF를 서버가 발행 → 서버 죽으면 안 꺼짐(mist를 펌웨어로 만든 이유와 반대)** | 중 |

세부:
- **fan**은 A가 쉬움 — 펌웨어에 `relay_pulse` 이미 있음. **취소**도 됨(`fan_off`가 진행중 타이머 취소 — relay 드라이버가 "수동 off 시 auto-off 예약 취소" 이미 구현).
- **heater**는 A든 B든 추가 작업(safety latch 타입이라 pulse 미구현).
- **남은시간 표시**: A는 앱이 "발행시각+duration"으로 계산(추가 작업 0). 정밀하게 하려면 telemetry에 remaining 추가.

**추천: A안 + fan 먼저.** 안전(펌웨어 자동 OFF)하고 mist와 통일됨. 단 앱이 `device_timers` 가정을 버려야 하므로 **앱팀과 조율 필수**. heater 타이머는 펌웨어 pulse 추가 후 2단계.
**이 결정이 푸는 것**: 앱의 `RunningTimer` 칩(이미 만들어둠)이 실제로 동작.

### 결정 ③ — LED 밝기 → 재검증 결과 **결정 불필요, 구현 가능**

**정정**: 처음엔 "하드웨어 미지원이라 on/off로 축소할지 결정 필요"라 했으나, 배선 재확인 결과 **하드웨어 변경 없이 펌웨어만으로 밝기 PWM 구현 가능**하다 (위 §2 요청 4 참조). LED_GPIO가 MOSFET 보드 + LEDC 인프라 존재.

→ **선택지 없음. 그냥 구현.** 앱은 이미 `led_on`+`{brightness:0~100}`을 보내고 있어 계약도 맞음. 펌웨어에서 LED를 relay→LEDC로 바꾸고 duty 제어만 추가하면 앱 슬라이더가 즉시 살아남. (앱 UI 수정 불필요.)

### 결정 ④ — commands 출처 기록: 컬럼 추가 vs 서비스계정 (요청 5)

**무엇을 정하나**: 감사 로그에 "이 명령을 누가·왜 냈나(수동/예약/타이머/가드)"를 어떻게 남길지.

배경: 지금 `commands`엔 `issued_by`(누가)뿐. 예약도 `issued_by=owner_id`라 **수동과 구분 안 됨**.

| 옵션 | 방식 | 장점 | 단점 |
|---|---|---|---|
| **A. 컬럼 추가** | `source(manual\|schedule\|timer\|guard)` + `source_id(uuid)` + `reason(text)` | PRD 감사로그 **필터 4종 + 상세이유** 전부 충족. 확장성 | 마이그레이션 1건 + 발행부 수정(작음) |
| **B. issued_by를 서비스계정** | 예약 발행 시 issued_by를 전용 UUID로 | 스키마 변경 없음 | schedule/timer/guard **구분 못함**(전부 같은 계정), `reason` 못 남김 → **필터 4종 중 2종만**. 반쪽 |

**추천: A안.** B는 PRD 요건을 절반만 채움. 스키마만 확정하면 마이그레이션 즉시.
**이 결정이 푸는 것**: 앱 감사로그 화면(출처 필터 + "목표 습도 50% 도달" 같은 이유 표시).

---

## 3. 내 문서 수정 완료 (`APP_TIMER_MIST.md`)

- ✅ `result` 값: `rejected_unknown_action` → `unknown_action`, `error` 행 추가, 전체 어휘(+`locked`/`expired`/`duplicate`) 명시
- ✅ KST 예제: `.toUtc().add(9h)` → `.toLocal()` (이중변환 버그 수정, 앱 지적 반영)
- ✅ 체크리스트 result 표기 수정

---

## 4. 다음 액션 (백엔드/펌웨어)

**결정 확정 (2026-08-12)**: ① 단계분리(skip=서버, stop=펌웨어) · ② A안 duration_ms + fan 먼저 · ③ LED PWM 펌웨어 구현 · ④ source 컬럼 추가. 아래는 확정 로드맵.

### 1순위 — 즉시 착수 (결정 불필요, 작음) ✅ 완료
- [x] **[BE]** `schedules` 화이트리스트에 `heater_on/off`, `fan_on/off`, `relay_on/off` 추가 (요청 1)
- [x] **[BE]** `APP_INTEGRATION.md §3.2`에 on/off 계열 액션 문서화

### 2순위 — 백엔드 (마이그레이션/러너) ✅ 완료
- [x] **[BE]** 요청 5: `commands`에 `source`/`source_id`/`reason` 컬럼 (마이그레이션 `2026-08-12_commands_source_and_guard.sql`) + `schedule_runner`가 `source='schedule', source_id=schedules.id` 채움
- [x] **[BE]** 요청 2(skip형): `schedule_runner`가 발행 직전 최신 telemetry 조회 → `guard`(skip류) 평가 → 스킵 시 발행 안 하고 `status='skipped', source='guard', reason=...` 감사 기록
- [x] **[BE]** `schedules`에 `guard` JSONB 필드 + 라우터 검증(GUARD_TYPES)

### 3순위 — 펌웨어 (코드 완료, **빌드/플래시 필요**)
- [x] **[FW]** 요청 3(fan 타이머): `fan_on`이 `payload.duration_ms` 받아 `relay_pulse`로 자동 OFF, 최대 2h. `fan_off`가 취소
- [x] **[FW]** 요청 4(LED 밝기): `light_pwm` 모듈 신규(LEDC TIMER_1/CH_1, **1kHz** — 옵토절연 대응). LED relay→PWM 전환, `led_on`+`brightness`(0~100). **MOSFET 보드만**; 릴레이 보드는 on/off 유지. 실물 dim 확인 필요
- [ ] **[FW]** 요청 2(stop형) + heater 타이머: heater 조건정지/타이머 (2단계, 안전 직결). **이 보드는 heater 미탑재라 heater 있는 보드에서 진행**

> ⚠️ 펌웨어(terra-iot-nano)는 코드만 반영됨. **`idf.py build flash` 필요** (이 환경엔 툴체인 없음). MQTT RX 버퍼는 현 payload(brightness/duration)엔 충분(작음).
> ⚠️ 백엔드 마이그레이션 `2026-08-12_commands_source_and_guard.sql` **Supabase 적용 필요**.

### 앱 조율 필요
- [ ] **[APP↔BE]** 요청 3: 앱이 가정한 `device_timers` 테이블 대신 **A안(duration_ms)**으로 감 → 앱의 `RunningTimer` 소스를 "발행시각+duration 계산"으로 변경 조율

**PRD 조정 제안 수용**: 부무 1/2/3초 방식(§4.2.2 "길이 설정 불가"를 뒤집음) — 백엔드/펌웨어 `mist`가 이미 그렇게 구현됨. 앱 채택 동의.
