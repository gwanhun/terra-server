# 백엔드 회신 — 분무 분사 시간: 5초 / 10초, 10초는 서버가 5초×2 로 분할 (2026-09-23)

> **회신 대상**: 앱(Flutter) 0.131.0 분무 분사 시간 변경 요청 (2026-09-23)
> **작성**: terra-server 백엔드 담당
> **결정(2026-09-23, 카톡 공유)**: 칩은 **5초 / 10초** 둘 (3·7초 제외). 기기 펌웨어는 당장 재플래시가
> 어려워 **손대지 않고**, 10초는 **서버가 5초 두 번**으로 채웁니다. 앱 변경은 칩 구성과 버튼 잠금 시간뿐입니다.
> **관련 커밋**: `305dcca`(telemetry capabilities), `35146c8`(허용값), 이번 커밋(분할 발행)
> **계약 정본**: [APP_TIMER_MIST.md §1.4](APP_TIMER_MIST.md), [MQTT.md](MQTT.md) 0.5.4

---

## 0. 요청 → 처리 대조

| # | 요청 | 처리 | 앱이 할 것 |
|:---:|---|---|---|
| 1 | 허용값에 5000/7000/10000 추가 | ✅ 즉시 명령·예약 모두. 옛 값 1000/2000/3000 유지, 7000 도 호환으로 받음 | 칩을 **5초 / 10초**로 (§1) |
| 2 | 펌웨어 상한 10000ms | ⏸️ **보류** — 재플래시 어려움. 대신 서버가 분할 (§2). 패치는 부록 A 에 보관 | — |
| 3 | 배포 순서 | ✅ 무관 — 펌웨어 변경 없음. 서버 배포 즉시 동작 | — |
| 4 | `capabilities.mist_max_ms` 보고 | ✅ 서버 수신 경로는 준비됨(`305dcca`)이나 **앱은 볼 필요 없음** — 분할이 기기별로 자동 | 칩 숨김 로직 불필요 |

---

## 1. 요청 1 — 허용값과 칩 구성

- 서버 화이트리스트: `{1000, 2000, 3000, 5000, 7000, 10000}`. **앱 칩은 5000·10000 만** 노출해 주세요.
  1000/2000/3000 은 저장된 옛 예약용, 7000 은 0.131.0 이 이미 배포돼 있어 400 을 맞지 않게 둔 호환값입니다(5+2 로 분할됨).
- 즉시 명령·예약 모두 형식만 검증합니다. 기기 상한 초과는 거절하지 않습니다 — 서버가 채우기 때문입니다(§2).
- 예약 편집기 기본값은 **5초**를 권합니다 (10초는 §2.2 의 "약 2초 쉼"이 있음).

---

## 2. 10초는 어떻게 되나 — 서버 분할 발행

### 2.1 동작

현재 기기 펌웨어는 분무 1회 상한이 **5초**입니다(`MIST_MAX_MS 5000`, 초과는 **조용히 5초로 clamp** — 말씀하신 "10초인 줄 알았는데 5초" 위험). OTA 가 없어 당장 플래시가 어려우므로 서버가 나눕니다:

```
앱: mist 10000 (행 1개, payload 그대로 저장)
서버: 기기 상한(capabilities.mist_max_ms, 미보고=5000) 확인
  → 기기에 mist 5000 발행 ─ 펌웨어 타이머로 5초 후 OFF
  → 나머지 5000 을 후속 명령으로 예약 (issued_at = 지금 + 5.0s + 1.5s)
  → 약 6.5~7.5초 뒤 mist 5000 발행 ─ 5초 분사
결과: 5초 · 약 2초 쉼 · 5초  (총 ~12초, 물 양은 10초분)
```

**안전한 이유**: 분사마다 펌웨어 타이머가 끕니다. 후속 명령이 유실돼도 "물이 덜 나옴"이지 펌프가 켜진 채 남지 않습니다.
(`relay_on`/`relay_off` 로 흉내 내는 방식은 off 유실 = 침수라 쓰지 않았습니다.)

### 2.2 앱에서 보이는 것

| 항목 | 내용 |
|---|---|
| 후속 명령 행 | `commands` 에 **별도 행** — `action=mist`, `payload.duration_ms=5000`, **`source='timer'`**, `reason="mist split of <원 id> (+5000ms)"`, `issued_by` 동일. 명령 이력에 보입니다. 푸시는 안 나갑니다(푸시는 `source=schedule` 만) |
| 원 명령 상태 | 첫 분사 ack 로 `acked`/`ok`. 후속 행은 자기 ack 로 별도 `acked` |
| 사용자 체감 | 10초 선택 시 **중간에 약 2초 멈춤**. 카톡으로 안내드린 부분 |
| **버튼 잠금** | **총 분사 시간 + 3초** (10초 → 13초). 후속 분사 중 재요청은 펌웨어 `busy` |
| 예약 | 10초 예약도 발화 시 동일하게 분할 |
| 신 펌웨어가 나중에 올라오면 | `mist_max_ms ≥ 10000` 보고 → 나누지 않고 한 번에. 기기별 자동, 앱 변경 없음 |

### 2.3 요청 4 — 앱은 `mist_max_ms` 를 볼 필요가 없습니다

분할이 서버에서 기기별로 자동이라 **칩 숨김 로직이 필요 없습니다.** 5초·10초 둘 다 항상 노출하시면 됩니다.
(수신 경로는 `305dcca` 로 준비돼 있어, 나중에 펌웨어가 보고하면 그때부터 한 번에 갑니다.)

---

## 3. 배포

| 대상 | 상태 |
|---|---|
| 서버: 허용값 + 분할 발행 + 예약 발행 | ✅ main — 운영 재시작(api+bridge) 후 유효 |
| 펌웨어 | 변경 없음 (보류). 부록 A 패치는 나중에 플래시할 때 |
| 앱 | 칩 5/10초, 버튼 잠금 총시간+3초, 예약 기본 5초 권장 |

---

## 4. 정리 — 앱측 조치

1. 칩 **5초 / 10초** (3·7초 제거), 예약 편집기 기본 5초 권장 (§1)
2. 버튼 잠금 **총 분사 시간 + 3초** (§2.2)
3. 명령 이력에 `source='timer'` 후속 행이 보임 — 필요하면 "자동 이어 분사" 정도로 표기 (§2.2)
4. `mist_max_ms` 기반 칩 숨김은 **불필요** (§2.3)

---

## 5. (참고) 펌웨어를 나중에 고칠 때 — 10초 연속 구동 안전성

### 5.1 코드에 남은 실측 기록

10초 연속 구동을 **직접 측정한 기록은 없습니다.** 대신 브라운아웃 관련 실측·대응이 펌웨어에 상세히 남아 있어 그대로 옮깁니다:

| 날짜 | 실측 | 대응 |
|---|---|---|
| 2026-09-17 | WiFi `PS_NONE`+15dBm 조합에서 **펌프 ON 1.7초 뒤 브라운아웃 리셋** (5V 벅컨버터 급전, terra-iot-07) | WiFi `MIN_MODEM` + TX 8.5dBm 으로 전류 피크 완화 (`wifi.c`) |
| 2026-09-17 | 리셋 중 GPIO hold 로 펌프가 계속 돌아 **리셋 연쇄 → 17초 블랙아웃** | ON 중 hold 해제, 부팅 시 펌프 강제 OFF (`relay.c`, `main.c`) |
| 2026-09-18 | 펌프 duty 시험: 40% 안 돎, 50~55% 약함, **100% 는 물 채우면 브라운아웃** | **run_duty 60%** + 소프트스타트 200ms 램프 (`main.c relay_cfg`) |

말씀하신 09-07 "~25초 통신 끊김" 은 위 9/17 의 리셋 연쇄와 같은 현상으로 보이며, **분사 길이가 아니라 기동 순간(돌입전류 + WiFi 피크)** 이 원인이었습니다. 10초 분사도 기동은 한 번이라 이 축의 위험은 늘지 않습니다.

남는 축은 **지속 전압 강하**(60% duty 로 10초)입니다. 100% 에서 브라운아웃이 났던 걸 보면 60% 도 전원이 약한 개체에선 여유가 크지 않을 수 있습니다. 그래서 **플래시 전 벤치**를 넣었습니다(부록 A 주석):

```
07 보드, 물 채운 상태: mist 10000 × 5회 연속(각 분사 후 12초 대기)
확인: 시리얼 reset_reason ≠ 9(BROWNOUT), WiFi/MQTT 끊김 없음, 서버 last_seen 연속
실패 시: run_duty 55% 로 낮추거나 MIST_MAX_MS 7000 으로 (서버는 보고값을 그대로 따르므로 코드 변경 없이 7초까지만 허용됨)
```

> 참고: 기기 telemetry 에는 카메라와 달리 `uptime`/`reset_reason` 이 없어 **브라운아웃 리셋을 원격에서 볼 수 없습니다.**
> 벤치가 시리얼 기준인 이유입니다. 필드에서 10초 분사 후 리셋이 의심되면 `last_seen_at` 공백(3초 주기)으로만 간접 추정됩니다 —
> 이 필드 추가는 별도 제안으로 남깁니다.

---

## 부록 A — 펌웨어 패치 (terra-iot-supermini, 4파일)

```diff
diff -ruN a/main/include/command_dispatch.h b/main/include/command_dispatch.h
--- a/main/include/command_dispatch.h	2026-09-23 11:47:57
+++ b/main/include/command_dispatch.h	2026-09-23 11:47:57
@@ -17,6 +17,13 @@
  * 검증 순서: JSON 파싱 → action 필드 존재 → TTL → dedup (최근 8개) → dispatch → ack.
  */
 
+/* 물분무 1회 최대 지속(ms). 명령 clamp 와 서버 보고(capabilities.mist_max_ms)가 같은 값을 쓴다.
+ * 5000 → 10000 (2026-09-23, 앱 0.131.0 분사 시간 5/7/10초). 서버는 이 값 이하만 허용하므로
+ * 여기만 올리면 구 서버·구 앱과도 안전하다(작은 값은 그대로 통과).
+ * ⚠️ 올리기 전 07 벤치: 10초 × 5회 연속 분사 후 reset_reason=9(BROWNOUT) 없는지 확인.
+ *    펌프 run_duty 60% 기준(main.c relay_cfg). 100% 는 물 채운 상태에서 브라운아웃(9/18). */
+#define MIST_MAX_MS 10000
+
 typedef struct {
     relay_handle_t     relay;
     relay_handle_t     fan;
diff -ruN a/main/src/cloud_client.c b/main/src/cloud_client.c
--- a/main/src/cloud_client.c	2026-09-23 11:47:57
+++ b/main/src/cloud_client.c	2026-09-23 11:47:57
@@ -1,4 +1,5 @@
 #include "cloud_client.h"
+#include "command_dispatch.h"   /* MIST_MAX_MS */
 
 #include <string.h>
 #include <stdlib.h>
@@ -195,6 +196,7 @@
     if (cap) {
         cJSON_AddStringToObject(cap, "board", "mosfet");
         cJSON_AddBoolToObject(cap, "led_dimmable", true);
+        cJSON_AddNumberToObject(cap, "mist_max_ms", MIST_MAX_MS);   /* 앱 7·10초 분무 칩 노출 근거 */
     }
 
     char *s = cJSON_PrintUnformatted(root);
diff -ruN a/main/src/command_dispatch.c b/main/src/command_dispatch.c
--- a/main/src/command_dispatch.c	2026-09-23 11:47:57
+++ b/main/src/command_dispatch.c	2026-09-23 11:47:57
@@ -15,7 +15,6 @@
 static const char *TAG = "cmd_dispatch";
 
 /* 물분무 최대 지속시간 (ms) — 서버 이상값/침수 방지 하드 상한. */
-#define MIST_MAX_MS 5000
 
 /* 팬 일회성 타이머 최대 (ms) — 2시간. 서버 이상값 방지 하드 상한. */
 #define FAN_TIMER_MAX_MS (2u * 60u * 60u * 1000u)
@@ -116,8 +115,9 @@
         *state_out = "SPRAY";
     } else if (strcmp(action, "mist") == 0 && s_act.relay) {
         /* 물분무: payload.duration_ms 만큼 펌프 ON 후 자동 OFF (relay_pulse, 논블로킹).
-         * 서버가 duration_ms 를 {1000,2000,3000} 으로 검증하지만, 여기서도 MIST_MAX_MS 로
-         * 상한 clamp 해 이중 방어. 하드웨어(릴레이/MOSFET) 차이는 relay 드라이버가 흡수. */
+         * 서버가 duration_ms 를 화이트리스트 + capabilities.mist_max_ms 로 검증하지만, 여기서도
+         * MIST_MAX_MS(command_dispatch.h) 로 상한 clamp 해 이중 방어. 서버에 보고하는 값과
+         * 같은 매크로라 "10초인 줄 알았는데 5초" 가 생길 수 없다. 하드웨어 차이는 relay 드라이버가 흡수. */
         const cJSON *jdur = cJSON_GetObjectItemCaseSensitive(root, "duration_ms");
         uint32_t ms = cJSON_IsNumber(jdur) ? (uint32_t)jdur->valuedouble : 0;
         if (ms == 0) {                          /* duration 누락/0 → 잘못된 요청 */
diff -ruN a/main/src/mqtt_app.c b/main/src/mqtt_app.c
--- a/main/src/mqtt_app.c	2026-09-23 11:47:57
+++ b/main/src/mqtt_app.c	2026-09-23 11:47:57
@@ -255,7 +255,11 @@
                  mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
     }
 
-    char payload[448];
+    /* capabilities: 페어링 body 와 같은 객체를 telemetry 에도 싣는다. 베타 기기는 콘솔 발급
+     * 자격증명으로 붙어 페어링을 호출하지 않으므로 서버 devices.capabilities 가 콘솔 기본값에
+     * 묶여 있었다. 서버(2026-09-23+)는 값이 바뀔 때만 UPDATE 하고 구 서버는 키를 무시한다.
+     * mist_max_ms: 앱이 7·10초 분무 칩 노출을 결정하는 근거(앱 0.131.0). */
+    char payload[512];
     int n = snprintf(payload, sizeof(payload),
         "{"
           "\"ts\":%lld,"
@@ -265,7 +269,8 @@
           "\"fan\":\"%s\","
           "\"fan2\":\"%s\","
           "\"heater\":{\"state\":\"%s\",\"locked\":%s},"
-          "\"hw_id\":\"%s\""
+          "\"hw_id\":\"%s\","
+          "\"capabilities\":{\"board\":\"mosfet\",\"led_dimmable\":true,\"mist_max_ms\":%d}"
           "%s"
         "}",
         (long long)now,
@@ -277,6 +282,7 @@
         heater_on ? "ON" : "OFF",
         heater_locked ? "true" : "false",
         hw_id,
+        MIST_MAX_MS,
         led_field);
 
     if (n < 0 || n >= (int)sizeof(payload)) {
```
