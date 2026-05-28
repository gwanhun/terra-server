# REST API 명세

> 베이스 URL: `https://api.example.com` (Lightsail nginx 리버스 프록시 경유)
> 인코딩: UTF-8, JSON
> CORS: `.env` 의 `APP_ORIGINS` 허용
>
> **인터랙티브 문서**: `GET /docs` (Swagger UI) · `GET /redoc` (ReDoc) · `GET /openapi.json` (OpenAPI 3 스키마)
> 로컬: `uv run uvicorn backend.main:app --reload` 후 http://localhost:8000/docs

## 1. 인증 모델 (3가지)

| 인증 방식 | 헤더 | 발급처 | 사용처 |
|----------|------|--------|--------|
| **JWT (사용자)** | `Authorization: Bearer <jwt>` | Supabase Auth (모바일/웹 앱 로그인) | 거의 모든 사용자 엔드포인트 |
| **Pair Token (1회용, RPi 전용)** | `Authorization: Bearer <pair_token>` | `POST /cameras/prepare-pair` 응답 (TTL 5분) | RPi 카메라 QR 페어링 (BLE 미사용 시) |
| **Camera Token** | `Authorization: Bearer <camera_token>` | `POST /cameras/pair` 응답 (1회) | 카메라 자체 호출 (영상 업로드) |
| **Device Token** | `Authorization: Bearer <device_token>` | `POST /devices/pair` 응답 (1회) | 디바이스 자체 호출 (펌웨어 업데이트 등, 추후) |

### Dev 모드 (`AUTH_MODE=dev`)

JWT 검증 스킵, `DEV_USER_ID` 환경변수의 UUID 를 모든 요청에 적용. **프로덕션 절대 금지.**

### 인증 실패 응답

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{ "detail": "Authorization 헤더가 없음." }
```

## 2. 엔드포인트 일람

### Stage 부트스트랩 + Stage F — ✅ 구현됨

| 메서드 | 경로 | 인증 | 설명 |
|--------|------|------|------|
| `GET` | `/health` | 없음 | 헬스 체크 |
| `POST` | `/devices/pair` | JWT | 디바이스 페어링 + MQTT 토큰 발급 |
| `GET` | `/devices` | JWT | 본인 디바이스 목록 |
| `GET` | `/devices/{id}` | JWT | 디바이스 단건 조회 |
| `PATCH` | `/devices/{id}` | JWT | 디바이스 이름/종 수정 |
| `DELETE` | `/devices/{id}` | JWT | 디바이스 삭제 |
| `POST` | `/enclosures` | JWT | 사육장 생성 |
| `GET` | `/enclosures` | JWT | 본인 사육장 목록 |
| `GET` | `/enclosures/{id}` | JWT | 사육장 단건 |
| `PATCH` | `/enclosures/{id}` | JWT | 사육장 수정 |
| `DELETE` | `/enclosures/{id}` | JWT | 사육장 삭제 |
| `POST` | `/cameras/pair` | JWT | 카메라 워커 페어링 + camera_token 발급 |
| `GET` | `/cameras` | JWT | 본인 카메라 목록 |
| `GET` | `/cameras/{id}` | JWT | 카메라 단건 |
| `PATCH` | `/cameras/{id}` | JWT | 카메라 수정 |
| `DELETE` | `/cameras/{id}` | JWT | 카메라 삭제 |
| `POST` | `/cameras/{id}/clips/upload-url` | **Camera Token** | R2 presigned PUT URL 발급 |
| `POST` | `/cameras/{id}/clips` | **Camera Token** | 업로드 완료 후 모션 클립 메타 등록 |
| `GET` | `/enclosures/{id}/clips` | JWT | 사육장의 모션 클립 목록 (cursor pagination) |
| `GET` | `/clips/{id}/url` | JWT | 영상 재생용 presigned GET URL |
| `DELETE` | `/clips/{id}` | JWT | 클립 삭제 (R2 객체 + DB 행) |

> **참고 (RPi 워커)**: BLE 가 불안정한 RPi 환경은 별도 QR 페어링 흐름 (`POST /cameras/prepare-pair` + Pair Token) 필요. **현재 미구현.** ESP32-P4 는 BLE 5.0 안정적이라 위 `/cameras/pair` 그대로 사용.

### Stage G (라이브 스트리밍) — 미구현 (스펙)

#### G1: JPEG snapshot (의사 라이브)

| 메서드 | 경로 | 인증 | 설명 |
|--------|------|------|------|
| `POST` | `/cameras/{id}/snapshot-start` | JWT | 스트리밍 시작 (interval_ms, duration_sec) |
| `POST` | `/cameras/{id}/snapshot-stop` | JWT | 스트리밍 종료 |
| `POST` | `/cameras/{id}/snapshot` | **Camera Token** | 워커가 JPEG 업로드 (R2 PUT 결과 통보) |
| `GET` | `/cameras/{id}/latest-snapshot.jpg` | JWT | 가장 최근 JPEG presigned GET URL |

#### G2: WebRTC P2P (본격 라이브)

| 메서드 | 경로 | 인증 | 설명 |
|--------|------|------|------|
| `GET` | `/cameras/webrtc/config` | JWT | STUN/TURN 서버 정보 |
| `POST` | `/cameras/{id}/webrtc/offer` | JWT | SDP offer → 워커 (시그널링) |
| `POST` | `/cameras/{id}/webrtc/ice` | JWT | ICE candidate 추가 |
| `POST` | `/cameras/{id}/webrtc/close` | JWT | 세션 종료 |

### Stage C (명령) — 미구현

**앱은 Supabase 에 직접 INSERT.** REST API 불필요.

```javascript
// 앱 코드 예시
await supabase.from('commands').insert({
  device_id: '...',
  action: 'heater_toggle'
});
// → terra-bridge 가 Realtime 으로 감지 후 MQTT publish
```

이렇게 RLS + Realtime 으로 REST 우회 → 백엔드 코드 ↓ 단순성 ↑.

## 3. 엔드포인트 상세

### 3.1 `GET /health`

**요청**: 본문 없음, 인증 없음

**응답**:
```json
{ "ok": true, "service": "terra-api" }
```

---

### 3.2 `POST /devices/pair`

신규 ESP32-S3 (센서/제어) 페어링. 디바이스가 BLE로 사용자 JWT 받은 후 WiFi 연결되면 호출.

**요청**:
```http
POST /devices/pair HTTP/1.1
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "name": "거실 비어디드",
  "species": "bearded_dragon",
  "firmware_ver": "1.0.0"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `name` | string | ✓ | 사용자 지정 이름 (1~64자) |
| `species` | string | | 종 (32자 이내) |
| `firmware_ver` | string | | 펌웨어 버전 |

**응답** (201 Created):
```json
{
  "id": "a1b2c3d4-...",
  "device_id": "terra-a1b2c3d4",
  "mqtt_token": "Xa2b9C..."
}
```

- `id`: Supabase `devices.id` (UUID)
- `device_id`: MQTT client_id (펌웨어가 NVS 저장)
- `mqtt_token`: MQTT password 평문 (**1회만 노출**, NVS 저장 필수)

---

### 3.3 `GET /devices`

본인 소유 디바이스 목록 (페어링 시각 내림차순).

**요청**:
```http
GET /devices HTTP/1.1
Authorization: Bearer <jwt>
```

**응답** (200):
```json
[
  {
    "id": "a1b2c3d4-...",
    "device_id": "terra-a1b2c3d4",
    "name": "거실 비어디드",
    "species": "bearded_dragon",
    "firmware_ver": "1.0.0",
    "created_at": "2026-05-26T12:34:56Z",
    "last_seen_at": "2026-05-26T13:00:00Z",
    "is_online": true
  }
]
```

---

### 3.4 `GET /devices/{device_uuid}`

디바이스 단건 조회. 본인 디바이스가 아니면 404.

**응답** (200): `GET /devices` 항목 하나.

**에러** (404): `{ "detail": "device not found" }`

---

### 3.5 `PATCH /devices/{device_uuid}`

이름/종 수정.

**요청**:
```json
{
  "name": "방 비어디드",
  "species": "leopard_gecko"
}
```

전송된 필드만 업데이트 (exclude_unset).

---

### 3.6 `DELETE /devices/{device_uuid}`

디바이스 삭제. cascade 로 `device_settings`, `telemetry`, `commands`, `alerts` 도 삭제.

**응답** (204 No Content)

---

## 4. Stage F 엔드포인트 (계획)

### 4.1 `POST /enclosures`

사육장(상위 묶음) 생성.

**요청**:
```json
{
  "name": "거실 사육장",
  "species": "bearded_dragon",
  "note": "온도 너무 떨어지면 알림"
}
```

**응답** (201):
```json
{ "id": "...", "name": "...", "species": "...", "created_at": "..." }
```

---

### 4.2 `POST /cameras/pair`

신규 ESP32-P4 카메라 워커 페어링. BLE 로 사용자 JWT 받은 후 WiFi 연결되면 호출.
(devices/pair 와 동일 패턴)

**요청**:
```http
POST /cameras/pair HTTP/1.1
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "enclosure_id": "...",  // 옵션
  "name": "거실 카메라",
  "model": "esp32-p4",    // "esp32-p4" | "rpi-zero-2-w" 등
  "firmware_ver": "terra-cam-p4 0.1.0",
  "resolution": "HD",
  "fps": 24,
  "clip_sec": 10
}
```

**응답** (201):
```json
{
  "id": "...",
  "camera_id": "p4cam-a1b2c3d4",
  "camera_token": "Yc4d8E..."
}
```

`camera_token` 으로 이후 `/cameras/{id}/clips/*`, `/snapshot`, `/webrtc/*` 호출 시 Bearer 인증.
ESP32-P4 는 NVS 에 저장 (RPi 는 `/etc/terra-cam/config.json`).

> **RPi 워커 페어링** (별도 흐름): BLE 가 불안정하면 QR 토큰 사용 (`POST /cameras/prepare-pair` → pair_token → `POST /cameras/pair`). 본 명세 후속 버전에서 추가.

---

### 4.3 `POST /cameras/{camera_id}/clips/upload-url`

모션 영상 업로드 직전 호출. R2 presigned PUT URL 발급.

**요청**:
```http
POST /cameras/{camera_id}/clips/upload-url HTTP/1.1
Authorization: Bearer <camera_token>
Content-Type: application/json

{
  "started_at": "2026-05-27T13:00:00Z",
  "duration_sec": 10.0
}
```

**응답** (200):
```json
{
  "url": "https://...r2.cloudflarestorage.com/...?X-Amz-Signature=...",
  "key": "clips/2026/05/27/picam-a1b2c3d4/<clip_id>.mp4",
  "expires_in": 300
}
```

RPi 워커는 `url` 로 단순 HTTPS PUT (body 만 H.264 mp4 파일).

---

### 4.4 `POST /cameras/{camera_id}/clips`

R2 PUT 완료 후 메타 등록.

**요청**:
```http
POST /cameras/{camera_id}/clips HTTP/1.1
Authorization: Bearer <camera_token>
Content-Type: application/json

{
  "key": "clips/2026/05/27/picam-a1b2c3d4/<clip_id>.mp4",
  "started_at": "2026-05-27T13:00:00Z",
  "duration_sec": 10.0,
  "file_size": 1048576,
  "width": 1280,
  "height": 720,
  "fps": 24.0,
  "codec": "h264",
  "container": "mp4",
  "motion_score": 0.42
}
```

**응답** (201):
```json
{ "id": "<clip_id>" }
```

`motion_clips` 테이블에 INSERT → 앱이 Realtime push 수신.

---

### 4.5 `GET /clips/{clip_id}/url`

영상 재생용 presigned GET URL 발급.

**요청**:
```http
GET /clips/<clip_id>/url HTTP/1.1
Authorization: Bearer <jwt>
```

**응답** (200):
```json
{
  "url": "https://...r2.cloudflarestorage.com/...?X-Amz-Signature=...",
  "expires_in": 3600
}
```

앱이 `url` 로 직접 GET (스트리밍 재생).

---

## 4.6 Stage G1: 라이브 스트리밍 (JPEG snapshot)

### 4.6.1 `POST /cameras/{camera_id}/snapshot-start`

앱이 "라이브 보기" 진입 시 호출. 워커에 1초마다 JPEG 캡처+업로드 명령.

**요청**:
```http
POST /cameras/{camera_id}/snapshot-start HTTP/1.1
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "interval_ms": 1000,
  "duration_sec": 300
}
```

**응답** (200):
```json
{ "started": true, "expires_at": "2026-05-27T13:05:00Z" }
```

### 4.6.2 `POST /cameras/{camera_id}/snapshot-stop`

```http
POST /cameras/{camera_id}/snapshot-stop HTTP/1.1
Authorization: Bearer <jwt>
```

**응답** (200): `{ "stopped": true }`

### 4.6.3 `POST /cameras/{camera_id}/snapshot` (워커용)

워커가 JPEG 캡처 후 호출 → terra-api 가 R2 키 `snapshots/{camera_id}/latest.jpg` 에 PUT.

**요청**:
```http
POST /cameras/{camera_id}/snapshot HTTP/1.1
Authorization: Bearer <camera_token>
Content-Type: image/jpeg

<binary jpeg data>
```

**응답** (200): `{ "stored_at": "2026-05-27T13:00:01Z" }`

### 4.6.4 `GET /cameras/{camera_id}/latest-snapshot.jpg`

앱이 1초마다 polling. presigned GET URL 반환 (또는 302 redirect).

**응답** (302):
```http
HTTP/1.1 302 Found
Location: https://...r2.cloudflarestorage.com/snapshots/.../latest.jpg?X-Amz-Signature=...
```

또는 (200 + JSON):
```json
{ "url": "...", "expires_in": 60, "captured_at": "2026-05-27T13:00:01Z" }
```

## 4.7 Stage G2: WebRTC P2P 라이브 (계획)

### 4.7.1 `GET /cameras/webrtc/config`

STUN/TURN 서버 정보. 앱이 PeerConnection 초기화 시 사용.

**응답**:
```json
{
  "iceServers": [
    { "urls": ["stun:stun.l.google.com:19302"] },
    { "urls": ["turn:turn.example.com:3478"], "username": "...", "credential": "..." }
  ]
}
```

### 4.7.2 `POST /cameras/{id}/webrtc/offer`

앱이 PeerConnection createOffer 후 호출. terra-api 가 MQTT 로 워커에 전달.

**요청**:
```json
{
  "sdp": "v=0\r\no=- 123 ...",
  "type": "offer"
}
```

**응답** (200): `{ "session_id": "uuid", "answer": { "sdp": "...", "type": "answer" } }`

### 4.7.3 `POST /cameras/{id}/webrtc/ice`

ICE candidate 추가.

**요청**:
```json
{
  "session_id": "uuid",
  "candidate": "candidate:1 1 UDP 2122252543 ..."
}
```

### 4.7.4 `POST /cameras/{id}/webrtc/close`

```json
{ "session_id": "uuid" }
```

---

## 5. 에러 코드 표

| 코드 | 의미 | 본문 예시 |
|------|------|----------|
| 200 | 성공 | (도메인별 응답) |
| 201 | 생성 성공 | (생성된 리소스) |
| 204 | 성공 (응답 본문 없음) | |
| 400 | 잘못된 요청 (필수 필드 누락 등) | `{ "detail": "변경 필드 없음" }` |
| 401 | 인증 실패 | `{ "detail": "Authorization 헤더가 없음." }` |
| 403 | 권한 없음 (RLS/소유권 위반) | `{ "detail": "권한 없음" }` |
| 404 | 리소스 없음 | `{ "detail": "device not found" }` |
| 422 | Pydantic 유효성 검증 실패 | (FastAPI 자동 응답, field 별 detail) |
| 500 | 서버 내부 오류 | `{ "detail": "..." }` |

## 6. 클라이언트 예시

### cURL (디바이스 페어링)

```bash
curl -X POST https://api.example.com/devices/pair \
  -H "Authorization: Bearer eyJhbGc..." \
  -H "Content-Type: application/json" \
  -d '{"name":"거실 비어디드","species":"bearded_dragon"}'
```

### JavaScript (앱)

```javascript
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// 로그인 (JWT 자동 발급)
await supabase.auth.signInWithPassword({ email, password });
const { data: { session } } = await supabase.auth.getSession();
const jwt = session.access_token;

// 디바이스 페어링 (앱은 직접 호출 X — BLE 통해 ESP32 가 호출)
// 디바이스 목록 조회
const res = await fetch('https://api.example.com/devices', {
  headers: { Authorization: `Bearer ${jwt}` }
});
const devices = await res.json();

// 명령 발행 (REST 안 쓰고 Supabase 직접)
await supabase.from('commands').insert({
  device_id: devices[0].id,
  action: 'heater_toggle'
});
```

### Swift (iOS, 영상 재생)

```swift
let req = URLRequest(url: URL(string: "https://api.example.com/clips/\(clipId)/url")!)
req.setValue("Bearer \(jwt)", forHTTPHeaderField: "Authorization")

URLSession.shared.dataTask(with: req) { data, _, _ in
    let response = try! JSONDecoder().decode(ClipUrl.self, from: data!)
    // response.url 로 AVPlayer 재생
    let player = AVPlayer(url: URL(string: response.url)!)
    player.play()
}.resume()
```

## 7. Realtime 채널 (참고)

REST 외에 Supabase Realtime 으로 push 받는 채널:

```javascript
// 텔레메트리 실시간 구독
supabase
  .channel('telemetry')
  .on('postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'telemetry',
        filter: `device_id=eq.${deviceId}` },
      (payload) => {
        console.log('새 센서값:', payload.new);
      })
  .subscribe();

// 모션 클립 신규 알림
supabase
  .channel('motion_clips')
  .on('postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'motion_clips',
        filter: `enclosure_id=eq.${enclosureId}` },
      (payload) => {
        console.log('새 모션 영상:', payload.new);
      })
  .subscribe();

// 명령 상태 변경 (acked)
supabase
  .channel('commands')
  .on('postgres_changes',
      { event: 'UPDATE', schema: 'public', table: 'commands',
        filter: `device_id=eq.${deviceId}` },
      (payload) => {
        if (payload.new.status === 'acked') {
          console.log('명령 완료:', payload.new.result);
        }
      })
  .subscribe();
```

## 8. 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-05-26 | 0.1.0 | 최초 명세 (devices CRUD + Stage F 계획) |
| 2026-05-27 | 0.2.0 | 카메라 페어링 흐름 RPi/QR 기반으로 변경 (prepare-pair + pair_token) |
| 2026-05-27 | 0.3.0 | 메인 카메라 워커 ESP32-P4 로 변경 (BLE 페어링), Stage G(라이브 스트리밍) 엔드포인트 추가 |
| 2026-05-27 | 0.4.0 | **Stage F 구현 완료** — enclosures/cameras/clips 라우터 + R2 presigned URL + Camera Token Bearer 인증 (44 tests passing). Swagger UI 메타데이터 보강 (`/docs` 즉시 사용 가능) |
