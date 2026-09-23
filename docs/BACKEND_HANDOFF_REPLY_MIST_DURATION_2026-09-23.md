# 백엔드·펌웨어 회신 — 분무 분사 시간 5/7/10초 (2026-09-23)

> **회신 대상**: 앱(Flutter) 0.131.0 분무 분사 시간 변경 요청 (2026-09-23)
> **작성**: terra-server 백엔드 담당
> **성격**: 요청 1·4 **서버 반영 완료**(배포 시점은 §5). 요청 2 는 **펌웨어 패치 준비됨 + 벤치 테스트 대기**. 요청 3(배포 순서)은 **제약을 없앴습니다** — 서버가 기기 상한을 알고 거절하므로 어느 순서로 나가도 "10초인 줄 알았는데 5초" 가 생기지 않습니다.
> **관련 커밋**: `305dcca`(telemetry capabilities), 이번 커밋(분사 시간 게이트)
> **계약 정본**: [APP_TIMER_MIST.md §1.4](APP_TIMER_MIST.md) (신설), [MQTT.md](MQTT.md) 0.5.4

---

## 0. 요청 → 처리 대조

| # | 요청 | 처리 | 앱이 할 것 |
|:---:|---|---|---|
| 1 | 허용값 {1000,2000,3000,5000,7000,10000} | ✅ 즉시 명령·예약 모두. **단 5000 초과는 기기 `capabilities.mist_max_ms` 이하일 때만** (§1) | 거절 결과 `unsupported_duration` 처리 (§1.3) |
| 2 | 펌웨어 상한 10000ms + 10초 하드웨어 안전성 | 🔧 패치 준비(부록 A). **5초는 현재 펌웨어로 이미 동작**, 7·10초만 플래시 필요. 플래시 범위는 저희 결정 대기 (§5.1) | — |
| 3 | 배포 순서: 펌웨어 먼저 | ✅ **제약 해소** — 서버가 먼저 나가도 안전 (§3) | — |
| 4 | `devices.capabilities.mist_max_ms` 보고 | ✅ 서버 수신 경로 완료. **페어링이 아니라 telemetry 로** 받습니다 — 이유 §4 | `led_dimmable` 과 같은 방식으로 칩 노출 결정 |

---

## 1. 요청 1 — 허용값 확장, 그리고 왜 "기기 상한" 을 함께 봤나

### 1.1 그대로 여신 대로 뚫으면 생기는 일

말씀하신 두 번째 위험("서버를 풀어도 7·10초가 조용히 5초로 줄어듦")이 핵심입니다. 화이트리스트만 넓히면
**구 펌웨어 기기에 10초가 그대로 나가고 펌웨어가 5초로 clamp** 합니다. 순차 플래시 기간에는 구·신
펌웨어가 섞이므로, 이 상태를 앱 UI(요청 4)에만 의존해 막는 건 한 겹입니다.

### 1.2 그래서 서버가 기기 상한으로 거절합니다

`devices.capabilities.mist_max_ms` (신 펌웨어가 보고, 구 펌웨어는 없음 → 5000 으로 취급) 를 기준으로:

| 요청값 | 구 펌웨어 (미보고) | 신 펌웨어 (10000) |
|---|:---:|:---:|
| 1000 / 2000 / 3000 / 5000 | ✓ | ✓ |
| 7000 / 10000 | **거절** | ✓ |

**저장된 옛 예약(1/2/3초)은 어느 펌웨어에서도 계속 통과합니다** — 요청하신 대로.

거절은 **세 경로 모두**에서 일어납니다:

| 경로 | 거절 형태 |
|---|---|
| `POST /devices/{id}/mist` | 400, `detail`: "이 기기의 분무 상한은 5000ms (capabilities.mist_max_ms) — 10000ms 는 펌웨어 업데이트가 필요함" |
| `POST/PATCH /devices/{id}/schedules` | 400, 같은 사유 (매일 발화 때마다 거절되는 예약을 만들지 않게 생성 시점에 차단) |
| `commands` 직접 INSERT (방법 A) | 브리지가 발행 직전 `status=rejected`, `result=unsupported_duration` — Realtime 으로 1초 내 수신 |

### 1.3 앱에서 볼 것

- 새 `result` 값 **`unsupported_duration`** (`status=rejected`). 히터의 `unsupported_action` 과 같은 계열이며
  펌웨어 ack 가 아니라 서버 거절입니다. 문구 제안: "이 기기는 5초까지 — 펌웨어 업데이트 후 7·10초 사용 가능".
- 요청 4 대로 칩을 숨기면 사용자는 이 거절을 볼 일이 거의 없습니다. 이건 **안전망**입니다.

---

## 2. 요청 2 — 펌웨어 상한 + 10초 연속 구동 안전성

### 2.1 코드 변경 (부록 A, 4파일 24줄)

- `MIST_MAX_MS` 5000 → **10000**, 로컬 define 을 `command_dispatch.h` 로 올려 **clamp 와 서버 보고가 같은 매크로**를 쓰게 함 — 둘이 어긋나 "보고는 10초, 실제 5초" 가 되는 경로를 원천 차단.
- telemetry 와 페어링 body 의 `capabilities` 에 `mist_max_ms` 동봉.
- 분사 중 `mist` 재수신 → 지금처럼 `busy` 거절 (`relay_pulse` 의 `pulsing` 플래그, 변경 없음). 버튼 잠금 "분사 시간 + 2초" 적절합니다.

### 2.2 하드웨어 안전성 — 코드에 남은 실측 기록

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

## 3. 요청 3 — 배포 순서 제약 해소

§1.2 의 게이트 덕분에 **서버 → 펌웨어 순서로 나가도 안전**합니다. 구 펌웨어 기기에는 5000 초과가 한 건도 발행되지 않습니다.
반대로 펌웨어가 먼저 나가면(서버 구버전) 7·10초가 서버 화이트리스트에서 400 — 이것도 조용한 오동작이 아니라 명시적 거절입니다.

실제 순서: **서버 먼저 배포**(오늘) → 07 벤치 → 신 펌웨어 순차 플래시. 플래시된 기기부터 자동으로 7·10초가 열립니다.

---

## 4. 요청 4 — `mist_max_ms` 는 telemetry 로 받습니다

capabilities 는 원래 **페어링 body** 로만 들어왔는데, **베타 기기는 페어링을 호출하지 않습니다**
(콘솔이 발급한 자격증명을 `provision_creds.h` 로 넣어 MQTT 에 바로 붙음). 그 경로만 쓰면 베타 기기의
`capabilities` 는 콘솔 기본값 `{board, led_dimmable}` 에 영원히 묶여 `mist_max_ms` 가 안 들어가고, 앱은
신 펌웨어를 올려도 7·10초 칩을 계속 숨기게 됩니다.

그래서 펌웨어가 **telemetry 에도 같은 `capabilities` 객체**를 싣고(부록 A), 서버는 `devices.capabilities` 와
**다를 때만** UPDATE 합니다(3초 주기라 프로세스 캐시, 첫 건만 SELECT). 이미 배포됐습니다(`305dcca`).

앱: `capabilities.mist_max_ms` 가 없거나 `< 7000` 이면 7초 칩, `< 10000` 이면 10초 칩을 숨기면 됩니다.
기기 부팅 후 첫 telemetry(3초 내)에 채워지므로 `devices` Realtime UPDATE 를 받으면 자동 반영됩니다.

---

## 5. 배포 상태·예정

| 대상 | 상태 |
|---|---|
| 서버: 요청 4 수신 경로 | ✅ main (`305dcca`) |
| 서버: 요청 1 + 게이트 (`unsupported_duration`) | ✅ main (`35146c8`) — 운영 재시작 후 유효 |
| 펌웨어: 부록 A | 🔧 패치 준비. 플래시 **범위** 결정 대기 (§5.1) — 어느 쪽이든 07 벤치(§2.2) 선행 |
| 콘솔 | 분무 버튼 1/2/3/5/7/10초 |

### 5.1 펌웨어를 반드시 고쳐야 하나 — 아니요. 5초는 지금 됩니다

먼저 사실 하나: **5초 칩은 현재 펌웨어에서 이미 동작합니다.** clamp 가 `ms > 5000` 일 때만 걸리므로
5000 은 그대로 통과하고, 서버도 5000 까지는 기기 상한과 무관하게 허용합니다. 즉 **앱 0.131.0 을
배포하는 순간 5초는 전 기기에서 쓸 수 있고**, 7·10초만 펌웨어 플래시가 필요합니다.

그리고 플래시 비용이 작지 않습니다 — 기기 펌웨어에 **OTA 가 없어** 베타 테스터 집의 보드까지 전부
**물리 재플래시**입니다. 서버 게이트(§1.2) 덕분에 구·신 펌웨어가 섞여 있어도 안전하므로, 범위를
고를 수 있습니다:

| 옵션 | 내용 | 효과 | 비용 |
|:---:|---|---|---|
| 1 | **플래시 안 함** | 전 기기 5초. 7·10초 칩은 숨겨짐(앱이 `mist_max_ms` 없음을 보고) | 0 |
| 2 | **07 + 이후 신규/재플래시 기기만** ⭐ 권장 | 플래시된 기기부터 7·10초가 열림. 기존 베타는 다음 자연스러운 재플래시 때 | 벤치 1회 + 신규 보드 |
| 3 | **전량 플래시** | 요청하신 그림 | 테스터 보드 회수·방문 |

> **저희 결정 대기.** 결정되면 이 절을 갱신하겠습니다. 어느 쪽이든 서버·앱 쪽 변경은 없습니다 —
> 게이트가 기기별로 판단하므로 플래시된 기기부터 자동으로 열립니다.

---

## 6. 정리 — 앱측 조치

1. `result=unsupported_duration` (`status=rejected`) 문구 처리 (§1.3)
2. 칩 노출: `capabilities.mist_max_ms` 기준 (§4)
3. 예약 편집기 기본 7초는 신 펌웨어 기기에서만 저장 가능 — 구 펌웨어 기기에서 7초 예약 시 400 이 오므로 기본값을 기기 상한에 맞춰 낮추는 게 좋겠습니다 (예: 상한 5000 이면 기본 5초)
4. **당분간은 전 기기가 상한 5000 입니다** (§5.1) — 홈 분무 시트·예약 편집기 모두 `mist_max_ms` 가 없으면 **기본 5초**로 시작해 주세요. 플래시된 기기부터 값이 들어오며 7·10초가 자동으로 열립니다

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
