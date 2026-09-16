# 백엔드·펌웨어 회신 — LED 자동 시간, 예약 payload(밝기·자동 시간) (2026-09-16)

> **회신 대상**: 앱(Flutter) `2026-09-16-server-request-led-timer-schedule-payload.md`
> **작성**: terra-server 백엔드 · terra-iot 펌웨어 담당
> **성격**: 서버 코드 + 펌웨어 코드 실측 대조. 추측 없음, 근거는 파일:줄번호로 표기.
> **결론 먼저**: §2·§3 은 **그대로 쓰면 됩니다**. **§1(`led_on` + `duration_ms`)은 미지원이고,
> 더 나쁜 건 펌웨어가 `ok` 로 응답한다는 점입니다** — 앱도 서버도 실패를 감지할 수 없습니다.

---

## 0. 요약

| # | 항목 | 판정 | 앱측 조치 |
|---|---|---|---|
| §1 | `led_on` + `payload.duration_ms` | ⛔ **미지원**. 무시하고 켜기만 하며 **`result: "ok"`** | "자동 시간" 칩 제거 또는 비활성 (요청서 표의 "1 미지원" 행) |
| §2 | `schedules.payload.brightness` | ✅ **지원**. 저장·조회·발화·발행 전 구간 보존 | 그대로 |
| §3 | `schedules.payload.duration_ms` (`fan2_on`/`fan_on`) | ✅ **지원**. 펌웨어 상한 **2시간** | 그대로. 단 상한·릴레이 보드 주의(§3.3) |

추가로 저희가 발견해 알려드리는 것:

| # | 내용 | 영향 |
|---|---|---|
| A | **`heater_*` 는 예약 가능 목록에 있지만 펌웨어에서 항상 `unknown_action`** | 히터 예약을 만들면 100% 실패 |
| B | `fan2_*` 는 **릴레이 보드에 코드 자체가 없음** → `unknown_action` | 냉각팬 UI 노출 조건 필요 |
| C | 같은 원인(200ms 가드)이 경로에 따라 `busy` 또는 `error` 로 갈림 | 앱 문구 분기 시 주의 |
| D | `busy` 로 거절된 명령을 **같은 msg_id 로 재시도하면 `duplicate`** | 재시도는 새 명령으로 |
| E | `brightness: 0` 은 `led_on` 인데도 실제로 **꺼짐**(`state: "OFF"`) | 밝기 하한을 앱이 강제해야 |

---

## 1. §1 — `led_on` + `duration_ms` ⛔ 미지원

### 1.1 서버는 그대로 전달합니다 (문제 없음)

`commands.payload` 는 자유 JSONB 이고, 발행 단계에서 MQTT payload 에 그대로 병합됩니다
(`backend/mqtt/dispatcher.py` 의 payload 병합부). 서버는 `duration_ms` 를 **검증하지도, 깎지도 않습니다.**
서버 검증이 걸린 건 `mist` 뿐입니다 (`backend/command_service.py:36-42`, 허용 1000/2000/3000).

즉 앱이 보낸 값은 **펌웨어까지 온전히 도달합니다.** 문제는 그 다음입니다.

### 1.2 펌웨어가 조용히 무시합니다 — 그리고 `ok` 를 응답합니다

`led_on` 분기는 `terra-iot-nano/main/src/command_dispatch.c:203-213` 인데, **`duration_ms` 를 읽는 줄이 하나도 없습니다.** 동작은 이렇습니다.

1. `brightness` 만 읽어 `light_pwm_set()` 호출 → `ESP_OK`
2. 에러 분기를 타지 않으므로 **`result: "ok"`, `state: "ON"`** ack 발행
3. 타이머를 걸지 않음 → **LED 는 끄기 전까지 계속 켜져 있습니다**

레포 전체에서 `duration_ms` 를 읽는 곳은 `command_dispatch.c` 의 **세 줄뿐**이며 각각 `mist`(`:120`), `fan_on`(`:140`), `fan2_on`(`:163`) 입니다. LED 는 없습니다.

구조적으로도 불가능합니다. 타이머는 릴레이 드라이버의 one-shot(`actuators/relay.c:126` `esp_timer_start_once`)으로 구현돼 있는데, **MOSFET 보드의 LED 는 릴레이가 아니라 `light_pwm` 이고 그 구조체에는 타이머 필드가 자체가 없습니다** (`light_pwm.c:21-26`). 레포 전체에서 `esp_timer_start` 히트는 `relay.c:126` 한 곳입니다.

릴레이 보드(`terra-iot-nano-relay`)도 마찬가지입니다. 여기선 LED 가 릴레이 핸들이라 `relay_pulse()` 를 쓸 수 있었는데 `command_dispatch.c:178-180` 이 `relay_on()` 한 줄뿐이라 역시 미지원입니다.

### 1.3 요청하신 4개 질문에 대한 답

| 질문 | 답 |
|---|---|
| ① 펌웨어가 `duration_ms` 를 처리하는가? 상한은? | **처리 안 함.** 상한 개념 자체가 없음. (참고: 팬은 `FAN_TIMER_MAX_MS` = **2시간**, 분무는 `MIST_MAX_MS` = **5초**) |
| ② 팬 타이머 중 LED 타이머를 걸면 `busy` 인가? 슬롯은 액추에이터별인가? | **타이머 슬롯은 액추에이터별로 독립**입니다 (펌프·팬1·팬2 각 1개, `main.c` 의 `relay_create()` 3회). 기기당 하나가 아닙니다. 다만 **LED 는 슬롯이 아예 없어서** 이 질문은 성립하지 않습니다. 팬1 타이머 중 팬2 타이머는 정상적으로 걸립니다 |
| ③ 미지원이면 `bad_request`/`unknown` 이 오는가, 아니면 무시하고 켜기만 하는가? | **후자입니다. 무시하고 켠 뒤 `ok` 를 보냅니다.** 경고 로그조차 없습니다. **앱이 우려한 최악의 케이스가 맞습니다** — 앱도 서버도 "시간이 지나도 안 꺼짐"을 감지할 수 없습니다 |
| ④ 자동 OFF 뒤 `telemetry.led` 가 `OFF` 로 바뀌는가? | **자동 OFF 자체가 없으므로 계속 `ON` 으로 보고됩니다.** 참고로 팬/펌프는 정상 반영됩니다 — telemetry 는 상태를 캐시하지 않고 매 주기 핸들에서 직접 읽으므로(`main.c:301-310`) 타이머 만료 후 **최대 3초** 안에 `OFF` 로 바뀝니다 |

### 1.4 그래서 어떻게 할 것인가 — 세 가지 선택지

**(A) 앱이 칩 제거 — 지금 바로 가능, 권장**
요청서 표의 "1 미지원" 행 그대로입니다. 서버·펌웨어 변경 없이 즉시 정합해집니다.

**(B) 펌웨어에 LED 타이머 추가 — 근본 해결, 리드타임 있음**
- 릴레이 보드: `relay_on()` → `relay_pulse()` 로 교체. **한 줄 수준**입니다
- MOSFET 보드: `light_pwm` 에 one-shot 타이머 필드를 새로 넣어야 합니다. `relay.c` 의 pulse 구조를 그대로 옮기면 되지만 새 코드입니다
- 상한은 팬과 같은 **2시간**으로 맞추는 게 자연스럽습니다. **앱의 3시간(10,800,000ms) 옵션은 이 경우 2시간으로 clamp 됩니다** — 3시간을 살리려면 상수를 같이 올려야 합니다
- 펌웨어 배포가 끝나기 전까지 구버전 기기는 여전히 미지원이므로, 앱은 어차피 **`devices.capabilities` 같은 지원 플래그로 분기**해야 합니다

**(C) 서버가 지연 `led_off` 를 발행 — 가능하지만 권장하지 않음**
서버가 `led_on` ack 후 N밀리초 뒤 `led_off` 를 큐잉하는 방식입니다. 구버전 기기도 커버되는 게 장점입니다.
다만 이 구조는 분무 설계 때 **의도적으로 배제한 방식**입니다 — OFF 명령이 유실되면 영영 안 꺼집니다
(`specs/stage-h-timer-mist-push.md:26-28`). LED 는 펌프보다 위험이 작아 허용 여지는 있지만,
서버 재시작·네트워크 단절 시 취소 보장이 어렵고 (B) 가 끝나면 버릴 코드입니다.

**저희 권고는 (A) 로 즉시 정합을 맞추고, (B) 를 별도 과제로 잡는 것**입니다. (B) 를 진행하기로 하면
지원 플래그 이름(예: `capabilities.led_timer`)을 먼저 확정해 주세요. 앱 분기와 펌웨어 보고가 같이 가야 합니다.

---

## 2. §2 — `schedules.payload.brightness` ✅ 지원

### 2.1 서버: 전 구간 보존 확인

2026-08-12 질문 4("`schedules.payload` 가 `mist` 의 `duration_ms` 외 다른 키도 통과시키는가")의 답은 **통과시킵니다** 입니다. 실측 근거:

| 단계 | 동작 | 근거 |
|---|---|---|
| `POST /devices/{id}/schedules` | `payload` 를 그대로 INSERT | `backend/routers/schedules.py:230` |
| 검증 | **`mist` 만** `duration_ms` 검사. 다른 action 의 payload 는 무검증 통과 | `schedules.py:122-135` |
| `GET` 응답 | `ScheduleOut.payload` 로 반환 | `schedules.py:102` |
| `PATCH /schedules/{id}` | `payload` 수정 가능. `mist` 만 재검증 | `schedules.py:290-294` |
| 예약 실행 | `commands.payload` 에 그대로 전달 | `backend/schedule_runner.py:127-136` |
| MQTT 발행 | 발행 payload 에 병합 | `backend/mqtt/dispatcher.py` payload 병합부 |

회귀 테스트를 붙여뒀습니다 (`tests/test_schedules_api.py`, `tests/test_schedule_runner.py`, `tests/test_mqtt_dispatcher.py`). 이제 이 경로가 깨지면 CI 에서 잡힙니다.

### 2.2 요청하신 3개 질문에 대한 답

| 질문 | 답 |
|---|---|
| ① POST/PATCH 가 `payload.brightness` 를 저장하고 GET 이 돌려주는가? | **예.** 위 표 참조 |
| ② 예약 실행 시 `commands.payload` 에 그대로 실려 `led_on` + `brightness` 로 발행되는가? | **예.** 발화·발행 모두 원본 그대로 |
| ③ 릴레이 보드(`led_dimmable=false`)의 brightness 는 무시인가 400인가? | **무시입니다. 400 아닙니다.** 서버는 `led_dimmable` 을 확인하는 코드가 아예 없고(전수 확인), 릴레이 보드 펌웨어는 `relay_on()` 한 줄이라 `brightness` 를 파싱조차 하지 않고 `ok`/`ON` 을 보냅니다 (`terra-iot-nano-relay/main/src/command_dispatch.c:178-180`). **앱이 기대한 "무시(켜기만)" 가 맞습니다** |

### 2.3 MOSFET 보드의 실제 brightness 처리

`terra-iot-nano/main/src/command_dispatch.c:205-213`:

- **0~100 으로 조용히 clamp** 합니다. 음수는 0, 100 초과는 100. `bad_request` 가 아닙니다
- 필드가 없거나 숫자가 아니면 **기본 100%**
- **⚠️ `brightness: 0` 을 주면 `led_on` 인데도 실제로 꺼지고 `state: "OFF"` 로 응답합니다** (`:213`).
  앱 UI 의 하한이 20% 이므로 정상 경로에선 안 생기지만, 슬라이더 초기값·복원 로직에서 0 이 새면
  "켜기 예약이 실행됐는데 꺼져 있음" 이 됩니다. **앱에서 하한을 강제해 주세요.** 서버는 막지 않습니다

---

## 3. §3 — `schedules.payload.duration_ms` (냉각팬) ✅ 지원

### 3.1 요청하신 2개 질문에 대한 답

| 질문 | 답 |
|---|---|
| ① `schedules.payload.duration_ms` 를 `fan2_on`/`fan_on` 실행에 그대로 전달하는가? | **예.** §2.1 과 동일 경로 |
| ② 미전달이면 pair 방식으로 되돌려야 하는가? | **되돌릴 필요 없습니다.** 단건 + `duration_ms` 방식으로 진행하세요 |

### 3.2 펌웨어 동작 확인

`fan_on`(`command_dispatch.c:137-154`) 과 `fan2_on`(`:161-177`) 이 **동일 로직**입니다 (핸들만 다른 복붙 블록).

- `duration_ms` 가 숫자이고 0보다 클 때만 타이머 경로 진입
- **상한 2시간** (`FAN_TIMER_MAX_MS`, `:20`). 초과분은 조용히 clamp
- `relay_pulse()` → esp_timer one-shot → 만료 시 **자동 OFF**
- ack 의 `state` 가 `"TIMER"` 로 옵니다 (`duration_ms` 없이 켜면 `"ON"`). **앱이 타이머 적용 여부를 구분하는 데 쓸 수 있습니다**
- 타이머 진행 중 `fan2_off` 를 보내면 **예약된 자동 OFF 도 함께 취소**됩니다 (`relay.c:80-86`)
- 자동 OFF 후 `telemetry.fan2` 가 **최대 3초 안에** `OFF` 로 반영됩니다

앱이 쓰는 30분/1시간/2시간은 전부 상한 이내라 그대로 동작합니다.

### 3.3 ⚠️ 두 가지 주의

**(1) 릴레이 보드에는 `fan2` 자체가 없습니다.**
`terra-iot-nano-relay` 의 명령 디스패처에 `fan2_toggle`/`fan2_on`/`fan2_off` **코드가 존재하지 않습니다.**
보내면 `unknown_action` 이 돌아옵니다. 서버는 `fan2_*` 를 예약 허용 목록에 두고 있어(`schedules.py:48-55`) 예약 생성은 성공하지만 실행이 실패합니다.
앱은 **냉각팬 UI 노출을 `telemetry.fan2 != null` 로 판별**해 주세요 — 이미 전달드린 계약 그대로입니다 (`docs/APP_FAN2_2026-09-07.md` §4).

**(2) `busy` 는 "타이머 진행 중" 말고 다른 원인으로도 납니다.**
`busy` 는 두 경우에 나옵니다.
- 같은 액추에이터에 **이미 타이머가 걸려 있는데** 또 타이머 명령을 보냄 (`relay.c:111`)
- **직전 명령 후 200ms 이내** — min_toggle 가드 위반 (`relay.c:93-96`)

두 원인이 같은 값으로 오므로 구분할 수 없습니다. 게다가 **비타이머 경로(`fan_off`, `led_*`, `relay_on` 등)에서는 같은 200ms 가드 위반이 `error` 로 나옵니다.** 문구를 분기한다면 둘 다 "잠시 후 다시 시도" 계열로 묶는 게 안전합니다.

---

## 4. 추가 발견 — 알아두셔야 할 것

### 4.1 ⛔ `heater_*` 예약은 항상 실패합니다

서버의 예약 허용 목록에 `heater_on`/`heater_off` 가 들어 있습니다 (`backend/routers/schedules.py:48-55`).
그런데 **두 보드 모두 히터 핸들이 `NULL` 로 주입**돼 있어(`terra-iot-nano/main/main.c:597` 부근, 주석에 "heater 는 미사용 → NULL" 명시) `heater_*` 명령은 디스패처의 조건을 통과하지 못하고 **`unknown_action`** 으로 응답됩니다.

- `locked`(히터 안전잠금) result 도 코드에는 있지만 **도달 불가**입니다
- 즉 앱에서 히터 예약을 만들 수 있으면 사용자는 100% 실패하는 예약을 갖게 됩니다
- **앱에서 히터 관련 UI 를 노출하지 않는 게 맞습니다.** 서버 허용 목록은 향후 히터 하드웨어를 위해 남겨둡니다. 원하시면 서버에서 400 으로 막는 것도 가능하니 알려주세요

### 4.2 재시도는 새 명령으로 보내세요

펌웨어는 최근 `msg_id` 8개를 링버퍼로 기억해 중복을 거릅니다 (`command_dispatch.c:31-51`).
등록 시점이 **실행 후**라서, `busy` 나 `error` 로 거절된 명령도 기억됩니다.
**같은 `msg_id` 로 재시도하면 `duplicate` 만 돌아옵니다.** 재시도는 `commands` 에 새 행을 넣어 새 `msg_id` 로 보내세요.

### 4.3 JSON 파싱 실패 시 ack 자체가 없습니다

명령 JSON 이 깨지면 펌웨어가 로그만 남기고 ack 를 보내지 않습니다 (`command_dispatch.c:250-254`).
서버 입장에선 `sent` 상태로 남고 타임아웃 처리가 없습니다. 정상 경로에선 발생하지 않지만,
앱이 `commands` Realtime 으로 상태를 지켜본다면 **영원히 `sent` 인 케이스**가 있을 수 있습니다.

### 4.4 서버에서 고친 것 (이번 조사 중 발견)

`commands.payload` 가 MQTT 프로토콜 필드를 덮어쓸 수 있었습니다. 발행 단계에서 payload 를 통째로 병합해서, 예를 들어 `led_on` 예약의 payload 에 `{"action": "heater_on"}` 이 들어 있으면 **엉뚱한 명령이 발행**됐습니다. RLS 로 본인 기기에 한정되지만 명령 바꿔치기라 차단했습니다 (`msg_id`/`issued_at`/`ttl_sec`/`action` 네 키는 무시하고 경고 로그).

앱 동작에는 영향이 없습니다. payload 에 저 네 키를 넣지만 않으면 기존과 동일합니다.

---

## 5. 정리 — 앱측 조치

| 항목 | 조치 |
|---|---|
| LED "자동 시간" 칩 | **제거 또는 비활성 + 안내** (요청서 "1 미지원" 행) |
| LED 예약 밝기 슬라이더 | **그대로.** 단 하한 20% 를 앱에서 강제 (0 이면 꺼짐) |
| 냉각팬 예약 단건 + `duration_ms` | **그대로.** pair 로 되돌릴 필요 없음 |
| 냉각팬 UI 노출 조건 | `telemetry.fan2 != null` 로 판별 (릴레이 보드엔 fan2 없음) |
| 히터 UI | **노출하지 않기** (항상 `unknown_action`) |
| 재시도 | 같은 `msg_id` 재사용 금지, 새 명령으로 |
| `busy` / `error` 문구 | 둘 다 "잠시 후 다시 시도" 계열로 묶기 |

### 저희에게 결정을 알려주실 것

1. **LED 타이머를 펌웨어에 넣을지** (§1.4 (B)). 넣는다면 **지원 플래그 이름**과 **상한(2시간 vs 3시간)** 을 확정해 주세요
2. **히터 예약을 서버에서 400 으로 막을지** (§4.1)
