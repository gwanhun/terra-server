# 앱 통합 가이드 (모바일/웹 클라이언트)

> **앱 AI/개발자의 단일 진실 소스.** terra-server 백엔드와 어떻게 통신해야 하는지 한 페이지로.
> 참조 구현: [web/index.html](../web/index.html) — Vanilla JS 로 모든 흐름 (회원가입/로그인/CRUD/제어/Realtime/알림) 검증된 코드.
> REST API 전체 명세: [docs/API.md](API.md) (Swagger: `/docs`)
> 펌웨어 시각 명세: [docs/FIRMWARE_INTEGRATION.md](FIRMWARE_INTEGRATION.md)

## 0. 핵심 원칙 (3가지)

### 1) 인증 — Supabase Auth + JWT
회원가입/로그인은 **Supabase Auth 직접 호출** (terra-api 가 처리 X). 로그인 후 받은 `access_token` 을 모든 호출에 Bearer 헤더로.

### 2) 데이터 변경 두 경로
| 경로 | 사용처 | 이유 |
|------|--------|------|
| **REST (terra-api)** | 페어링, R2 presigned URL 등 서버 로직 필요한 것 | 토큰 발급, 외부 API 호출 |
| **Supabase 직접** (Postgres + RLS) | CRUD, 명령 발행, 알림 해제 | 단순 INSERT/UPDATE/SELECT — REST 거치면 중복 |

### 3) 실시간 = Supabase Realtime (WebSocket)
telemetry, alerts, commands, motion_clips 의 INSERT/UPDATE 를 **WebSocket 으로 push**. polling X.

---

## 1. 인증 (Supabase Auth)

### 클라이언트 초기화
```dart
// Flutter
import 'package:supabase_flutter/supabase_flutter.dart';

await Supabase.initialize(
  url: 'https://xxx.supabase.co',
  anonKey: 'eyJ... (publishable key)',
);
final sb = Supabase.instance.client;
```

```js
// JS
import { createClient } from '@supabase/supabase-js';
const sb = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
```

> **`SUPABASE_URL` + `SUPABASE_PUBLISHABLE_KEY` 만 클라이언트 공개 OK** (anon 권한 + RLS 보호).
> 백엔드에서 받는 방법: `GET https://api.terra-server.uk/web-config` → `{ supabaseUrl, supabasePublishableKey }`.

### 회원가입 / 로그인 / 로그아웃
```dart
await sb.auth.signUp(email: email, password: password);
await sb.auth.signInWithPassword(email: email, password: password);
await sb.auth.signOut();

// 현재 세션 (JWT 포함)
final session = sb.auth.currentSession;
final jwt = session?.accessToken;
```

> **JWT 유효 시간**: 기본 1시간. Supabase SDK 가 refresh_token 으로 자동 갱신.
> 자세한 흐름: [docs/API.md §1](API.md)

---

## 2. REST 호출 (terra-api)

### 베이스
- URL: `https://api.terra-server.uk`
- 모든 호출에 `Authorization: Bearer <jwt>` 헤더 (단 페어링 시 디바이스 토큰 별도)

### 자주 쓰는 endpoint
| 메서드 | 경로 | 인증 | 용도 |
|--------|------|------|------|
| `GET` | `/health` | 없음 | 서버 살아있는지 |
| `GET` | `/web-config` | 없음 | SUPABASE_URL + publishable key 자동 노출 |
| `POST` | `/enclosures` | JWT | 사육장 생성 |
| `GET` | `/enclosures` | JWT | 본인 사육장 목록 |
| `POST` | `/devices/pair` | JWT | **디바이스 페어링** — ESP32 가 호출 (BLE 흐름 끝점). 앱은 보통 직접 호출 X (※) |
| `GET` | `/devices` | JWT | 본인 디바이스 목록 (`is_online`, `last_seen_at` 포함) |
| `POST` | `/cameras/pair` | JWT | 카메라 페어링 (디바이스와 동일 패턴) |
| `GET` | `/cameras` | JWT | 본인 카메라 목록 |
| `GET` | `/clips/{id}/url` | JWT | 영상 재생용 presigned GET URL |
| `GET` | `/enclosures/{id}/clips` | JWT | 사육장의 모션 클립 목록 |

(※) 디바이스 페어링은 보통 ESP32 가 BLE 로 사용자 JWT 받은 후 직접 `POST /devices/pair` 호출. 자세한 BLE 흐름: [docs/FIRMWARE_INTEGRATION.md §2](FIRMWARE_INTEGRATION.md).

전체 명세: [docs/API.md](API.md) 또는 https://api.terra-server.uk/docs (Swagger UI, Basic Auth).

### 호출 예시
```dart
// Flutter
final res = await http.get(
  Uri.parse('https://api.terra-server.uk/devices'),
  headers: {'Authorization': 'Bearer ${sb.auth.currentSession!.accessToken}'},
);
final devices = jsonDecode(res.body);
```

```js
// JS
const res = await fetch('https://api.terra-server.uk/devices', {
  headers: { Authorization: `Bearer ${sb.auth.currentSession.access_token}` },
});
const devices = await res.json();
```

---

## 3. 디바이스 제어 — `commands` INSERT (Supabase 직접)

### 핵심 — REST 안 씀, **Supabase JS 로 직접 INSERT**

```dart
// Flutter
await sb.from('commands').insert({
  'device_id': deviceUuid,             // devices.id (UUID)
  'issued_by': sb.auth.currentUser!.id,
  'action': 'heater_toggle',           // 또는 다른 action
}).select().single();
```

```js
// JS
await sb.from('commands').insert({
  device_id: deviceUuid,
  issued_by: sb.auth.currentUser.id,
  action: 'heater_toggle',
}).select().single();
```

### action 종류 ([docs/MQTT.md §2](MQTT.md))

| action | 페이로드 추가 | 디바이스 동작 |
|--------|--------------|--------------|
| `relay_toggle` | — | 워터펌프 토글 |
| `fan_toggle` | — | 팬 토글 |
| `heater_toggle` | — | 히터 토글 (safety latch 활성 시 거부) |
| `heater_clear` | — | safety latch 해제 |
| `led_on` | — | LED 점등 |
| `led_up` | — | LED 밝기 + |
| `led_down` | — | LED 밝기 - |

### 흐름 (전체 사이클)
```
앱 → commands INSERT (status='pending')
    ↓ (1초 안)
terra-bridge dispatcher → MQTT publish → status='sent'
    ↓ (디바이스 응답 시간)
ESP32 → ack publish → terra-bridge handle_ack → status='acked', result, acked_at
```

→ 앱은 `commands` 테이블 Realtime 구독해서 status 변화 추적.

### 옵션 — TTL/payload
```dart
await sb.from('commands').insert({
  'device_id': deviceUuid,
  'issued_by': sb.auth.currentUser!.id,
  'action': 'token_rotate',
  'payload': {'new_token': 'newtoken123'},  // action 별 추가 필드
  'ttl_sec': 60,                             // 기본 10초
});
```

---

## 4. 실시간 — Supabase Realtime 구독

### 4.1 텔레메트리 (디바이스 센서값)

```dart
sb.channel('telemetry-rt')
  .onPostgresChanges(
    event: PostgresChangeEvent.insert,
    schema: 'public',
    table: 'telemetry',
    callback: (payload) {
      final row = payload.newRecord;
      // row.t_a, row.h_a, row.relay, row.fan, row.heater_state, ...
      // 디바이스별 최신값 캐시 갱신 → UI 업데이트
    },
  ).subscribe();
```

```js
sb.channel('telemetry-rt')
  .on('postgres_changes',
    { event: 'INSERT', schema: 'public', table: 'telemetry' },
    (payload) => {
      const row = payload.new;
      // 디바이스별 최신값 캐시 → UI
    })
  .subscribe();
```

> RLS 가 본인 디바이스만 필터. 다른 사용자 telemetry 는 안 옴.
> 페이로드 컬럼: [docs/DATABASE.md](DATABASE.md) 의 `telemetry` 테이블 + [docs/MQTT.md §1](MQTT.md).

### 4.2 명령 상태 변화 (pending → sent → acked)

```dart
sb.channel('commands-rt')
  .onPostgresChanges(
    event: PostgresChangeEvent.update,    // INSERT 는 본인이 발행한 거 알고 있으므로 UPDATE 만
    schema: 'public', table: 'commands',
    callback: (payload) {
      final cmd = payload.newRecord;
      // cmd.status: pending → sent → acked | rejected | expired
      // cmd.result: 'ok', 'rejected_locked' 등
      // cmd.acked_at: 디바이스 응답 시각
    },
  ).subscribe();
```

### 4.3 알림 (alerts)

```dart
sb.channel('alerts-rt')
  .onPostgresChanges(
    event: PostgresChangeEvent.all,   // INSERT + UPDATE (resolve 도 받기)
    schema: 'public', table: 'alerts',
    callback: (payload) {
      if (payload.eventType == PostgresChangeEvent.insert) {
        final a = payload.newRecord;
        // a.kind: temp_high / temp_low / humid_low / heater_latched / sensor_fault / offline
        // a.severity: info / warning / critical
        // a.message, a.context (jsonb)
        if (a['severity'] == 'critical') {
          // 푸시 알림 / 배너 표시
        }
      }
    },
  ).subscribe();
```

알림 종류 표는 [docs/ARCHITECTURE.md](ARCHITECTURE.md) 또는 [backend/alerts.py](../backend/alerts.py) 의 evaluate_telemetry 참조.

### 4.4 모션 클립 신규 (motion_clips)

```dart
sb.channel('clips-rt')
  .onPostgresChanges(
    event: PostgresChangeEvent.insert,
    schema: 'public', table: 'motion_clips',
    callback: (payload) {
      final clip = payload.newRecord;
      // clip.camera_id, clip.started_at, clip.duration_sec, clip.r2_key
      // → 사육장 화면에 새 영상 썸네일 즉시 표시
    },
  ).subscribe();
```

영상 재생: `GET /clips/{id}/url` → presigned URL → AVPlayer/`<video>` 에 src 로 박음.

---

## 5. 알림 해제 (`alerts` UPDATE)

```dart
await sb.from('alerts').update({
  'resolved_at': DateTime.now().toUtc().toIso8601String(),
}).eq('id', alertId);
```

RLS 정책상 본인 디바이스의 alerts 만 UPDATE 가능 (`auth.uid()` 자동 검증).

---

## 6. 디바이스 페어링 (앱 시점, BLE)

ESP32-S3 가 **자체 BLE 광고** → 앱이 BLE write 로 정보 전달 → ESP32 가 HTTPS POST 호출.

### 앱이 전달해야 할 데이터

```json
{
  "ssid": "MyWiFi",
  "password": "wifipass",
  "jwt": "<현재 사용자 access_token>",
  "name": "거실 비어디드"
}
```

### NimBLE 텍스트 프로토콜 ([FIRMWARE_INTEGRATION.md §2.1](FIRMWARE_INTEGRATION.md))

펌웨어가 GATT RX char (UUID `12345678-1234-1234-1234-123456789abe`) 에 받는 텍스트 명령:

```
SSID:<ssid>
PASS:<password>
NAME:<디바이스 이름>
JWT_BEGIN <length>           ← JWT 전체 길이 (십진수)
JWT:<chunk 1>                ← 200자 이내씩 쪼개기
JWT:<chunk 2>
...
JWT:<chunk N>                ← 누적 == length 도달 시 완성
CONNECT                       ← WiFi 연결 + 자동 페어링
```

### 앱 측 구현 패턴 (의사 코드)

```dart
final device = await flutterBlue.scan(serviceUuid: '12345678-...-abc');
final rxChar = device.getCharacteristic('...-abe');

await rxChar.write(utf8.encode('SSID:$ssid'));
await rxChar.write(utf8.encode('PASS:$password'));
await rxChar.write(utf8.encode('NAME:$deviceName'));

final jwt = sb.auth.currentSession!.accessToken;
await rxChar.write(utf8.encode('JWT_BEGIN ${jwt.length}'));
for (var i = 0; i < jwt.length; i += 200) {
  final chunk = jwt.substring(i, min(i + 200, jwt.length));
  await rxChar.write(utf8.encode('JWT:$chunk'));
  await Future.delayed(Duration(milliseconds: 50));  // BLE write 간격
}
await rxChar.write(utf8.encode('CONNECT'));
```

### TX char (notify, UUID `...-abd`) 응답 listen

| 메시지 | 의미 |
|--------|------|
| `NAME_OK` | NAME 저장됨 |
| `JWT_CHUNK <누적>/<전체>` | 진행 상황 |
| `JWT_OK <전체>` | JWT 누적 완료 |
| `WIFI_OK` / `WIFI_FAIL` | WiFi 연결 결과 |
| `ERR:NO_SSID` / `ERR:...` | 에러 |
| `PAIR_OK <device_id>` | 페어링 성공 |

---

## 7. 카메라 클립 재생

```dart
// 1. 사육장의 클립 목록
final clips = await http.get(
  Uri.parse('https://api.terra-server.uk/enclosures/$encId/clips'),
  headers: {'Authorization': 'Bearer $jwt'},
);

// 2. 특정 클립 재생용 URL
final urlRes = await http.get(
  Uri.parse('https://api.terra-server.uk/clips/$clipId/url'),
  headers: {'Authorization': 'Bearer $jwt'},
);
final {url, expires_in} = jsonDecode(urlRes.body);

// 3. video_player 에 박기
final controller = VideoPlayerController.networkUrl(Uri.parse(url));
await controller.initialize();
controller.play();
```

R2 presigned URL TTL 1시간. 그 안에 재생/시크 가능. 만료 후 재호출.

---

## 8. 흔한 시나리오

### 8.1 부팅 — 자동 로그인 + 초기 데이터 로드
```dart
final session = sb.auth.currentSession;
if (session == null) {
  // 로그인 화면
} else {
  // 1) 본인 사육장/디바이스/카메라 목록 (REST or Supabase select)
  // 2) Realtime 구독 시작 (telemetry, alerts, commands, motion_clips)
  // 3) 활성 알림 (alerts where resolved_at IS NULL) 패널 표시
}
```

### 8.2 사육장 상세 화면 — 하나의 사육장 통합 view
```dart
// 한 화면에서:
final enclosure = await sb.from('enclosures').select().eq('id', encId).single();
final devices = await sb.from('devices').select().eq('enclosure_id', encId);
final cameras = await sb.from('cameras').select().eq('enclosure_id', encId);
final recentClips = await http.get('/enclosures/$encId/clips?limit=10');
final activeAlerts = await sb.from('alerts')
  .select().is_('resolved_at', null)
  .in_('device_id', devices.map((d) => d['id']).toList());

// Realtime 구독으로 자동 업데이트
```

### 8.3 명령 발행 + 결과 추적
```dart
// 발행
final cmd = await sb.from('commands').insert({
  'device_id': dev['id'],
  'issued_by': sb.auth.currentUser!.id,
  'action': 'heater_toggle',
}).select().single();

// 발행 즉시 UI 에 "발행됨" 표시 (status=pending)
// commands-rt 구독 콜백에서 cmd.status 변화 받으면 UI 갱신:
//   pending → sent (1~2초): "전송됨"
//   sent → acked: "✓ 완료 (result=ok)"
//   sent → rejected_*: "✗ 거부 (사유 표시)"
//   pending → expired (TTL 후): "⌛ 시간초과"
```

---

## 9. 보안 / 권한 모델

| 데이터 | RLS 정책 | 의미 |
|--------|----------|------|
| `enclosures` / `devices` / `cameras` | `auth.uid() = owner_id` | 본인 것만 보고 수정 |
| `telemetry` / `alerts` | 본인 devices 의 telemetry 만 SELECT | 다른 사용자 데이터 못 봄 |
| `commands` INSERT | `auth.uid()` 가 본인 devices 이면서 `issued_by = auth.uid()` | 다른 디바이스에 명령 발행 불가 |
| `motion_clips` | 본인 cameras 의 클립만 SELECT/DELETE | 영상 격리 |

→ **앱이 잘못된 쿼리 하면 자동 거부** (Supabase 가 빈 결과 또는 403 반환). 클라이언트가 owner_id 신경 안 써도 됨.

---

## 10. 디버그 / 로컬 테스트

### 10.1 백엔드 동작 확인
- `GET https://api.terra-server.uk/health` → `{"ok":true,"service":"terra-api"}`
- `GET https://api.terra-server.uk/docs` → Swagger UI (Basic Auth 필요)
- `GET https://api.terra-server.uk/` → 테스트 콘솔 (web/index.html) — 앱 만들기 전에 모든 흐름 검증 가능

### 10.2 시뮬레이션
실제 ESP32 없이 디바이스 흐름 테스트:
```bash
cd ~/project/terra-server
uv run python scripts/sim_device.py --pair --jwt <jwt> --name "테스트앱"
# → 가짜 디바이스 등록 + 3초마다 telemetry publish + command 받으면 ack
```

앱에서 명령 발행 → 시뮬레이터가 ack → 앱의 commands-rt 콜백에서 status 변화 받음 → UI 검증.

---

## 11. 다음 단계 (미구현, 명세만)

| 항목 | 명세 | 상태 |
|------|------|------|
| **카메라 페어링 BLE 흐름** | 디바이스와 거의 동일 (NAME, JWT, MODEL 등) | 명세만 |
| **JPEG 라이브 스트리밍** | [docs/API.md §4.6](API.md) (Stage G1) | 미구현 |
| **WebRTC 라이브** | [docs/API.md §4.7](API.md) (Stage G2) | 미구현 |
| **FCM 푸시 알림** | critical alert → 백그라운드 푸시 | 명세만 (Stage D 후속) |
| **device_settings CRUD** | 사용자가 임계값 (alert_temp_high 등) 편집 | 미구현 — Supabase 직접 INSERT/UPDATE 로 가능 |

---

## 부록 A. 컴퓨터 간 통신 흐름 한눈에

```
[모바일 앱]
  │ Supabase JS / Flutter SDK
  ├──── HTTPS ──→ Supabase Auth (signUp/signIn)
  ├──── HTTPS ──→ Postgres REST (insert commands, select telemetry, ...)
  ├──── WSS  ──→ Supabase Realtime (telemetry/alerts/commands/clips push)
  ├──── HTTPS ──→ terra-api (pair / clips presigned URL / enclosures)
  └──── HTTPS ──→ R2 (영상 직접 GET, presigned URL)
        ↑
        │ presigned URL 발급
[terra-api] ────→ Cloudflare R2
```

ESP32 측 흐름은 [docs/FIRMWARE_INTEGRATION.md](FIRMWARE_INTEGRATION.md) 참조.

---

## 부록 B. 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-06-08 | 0.1.0 | 최초 작성 (앱 통합 단일 진실 소스) |
