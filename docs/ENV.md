# 환경변수 가이드

> 템플릿: [.env.example](../.env.example)

## 민감도 분류

| 등급 | 변수 | 취급 |
|------|------|------|
| 🔴 Critical | `SUPABASE_SERVICE_ROLE_KEY`, `MQTT_BRIDGE_PASSWORD`, `R2_SECRET_ACCESS_KEY`, `DOCS_BASIC_PASS` | 절대 커밋 금지, 유출 시 즉시 로테이션 |
| 🟠 Sensitive | `DEV_USER_ID`, `SUPABASE_URL`, `R2_ACCESS_KEY_ID`, `R2_ACCOUNT_ID`, `DOCS_BASIC_USER` | .env / Lightsail 내부에만 |
| 🟢 Non-secret | `MQTT_BROKER_HOST`, `AUTH_MODE`, `APP_ORIGINS`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL` | .env.example 에 기본값 공개 |

## 섹션별 설명

### Supabase

- `SUPABASE_URL` — 대시보드 > Settings > API > Project URL
- `SUPABASE_SERVICE_ROLE_KEY` — 같은 페이지 > service_role (secret) key
  - **RLS 바이패스 권한** → 절대 클라이언트(앱) 노출 금지
  - 유출 시 대시보드에서 즉시 로테이션
- `DEV_USER_ID` — Stage 초반 로컬 테스트용 하드코딩 UUID
  - Supabase 대시보드 > Auth > Users 에서 본인 UUID 복사

### 인증 모드

- `AUTH_MODE=dev` — JWT 검증 스킵, DEV_USER_ID 그대로 사용 (로컬/pytest)
- `AUTH_MODE=prod` — Authorization: Bearer 필수
  - 프로덕션 배포 직전 반드시 prod 로 전환

### Supabase JWT (AUTH_MODE=prod 시 필수)

- `SUPABASE_JWT_ISSUER` = `{SUPABASE_URL}/auth/v1`
- `SUPABASE_JWKS_URL` = `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`

### MQTT

- `MQTT_BROKER_HOST` — Mosquitto 호스트 (`localhost` 또는 도메인)
- `MQTT_BROKER_PORT` — TLS 포트 8883
- `MQTT_USE_TLS` — `true` (false 는 로컬 테스트 외 금지)
- `MQTT_CA_CERT_PATH` — Let's Encrypt 사용 시 빈 값 (시스템 CA), 자체 서명 시 PEM 경로
- `MQTT_BRIDGE_USERNAME` — 브리지 전용 계정 (모든 토픽 접근)
- `MQTT_BRIDGE_PASSWORD` — 🔴 강력한 비번. Mosquitto password file 에도 등록

### API 서버

- `APP_ORIGINS` — CORS 허용 origin (콤마 구분, 빈 값이면 `*`)
- `DOCS_BASIC_USER` / `DOCS_BASIC_PASS` — OpenAPI 문서 Basic Auth
  - `/docs`, `/redoc`, `/openapi.json` 셋 다 동일 가드. JWT 와 무관하게 항상 적용.
  - 둘 중 하나라도 비어있으면 모든 요청 401 → 사실상 문서 차단 효과 (실수 방지)
  - 강력한 비번: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`

### WebRTC 라이브 (Stage G2)

`/cameras/webrtc/config` 응답에 그대로 들어가 브라우저 `RTCPeerConnection({iceServers})` 에 전달됨.

- `WEBRTC_STUN_URLS` — STUN 서버 URL (콤마 구분). 기본 Google STUN.
- `WEBRTC_TURN_URLS` — TURN 서버 URL (옵션, 콤마 구분). 빈 값이면 응답에서 TURN 항목 누락 → STUN-only 동작.
- `WEBRTC_TURN_USERNAME` / `WEBRTC_TURN_CREDENTIAL` — TURN 자격증명. TURN URL 있을 때만 의미 있음.

#### 언제 TURN 이 필요?
대칭 NAT (양쪽 모두) 환경에서 P2P 가 직접 연결 못 함. 보통 모바일 셀룰러 → 카메라 (가정 IP) 시나리오. STUN 만으로 80% 정도는 통과, 안 되면 TURN relay 가 받쳐줌.

#### 자체 운영 (coturn)
```
WEBRTC_TURN_URLS=turn:turn.example.com:3478?transport=udp
WEBRTC_TURN_USERNAME=terra
WEBRTC_TURN_CREDENTIAL=...
```

### Cloudflare R2 (Stage F)

ESP32-CAM 모션 영상 저장용. S3 호환 API.

- `R2_ACCOUNT_ID` — Cloudflare 대시보드 우측 상단 Account ID (32자 hex). endpoint URL 조립용
- `R2_ENDPOINT` — `https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`
- `R2_ACCESS_KEY_ID` — R2 > Manage R2 API Tokens 에서 발급 (Object R/W)
- `R2_SECRET_ACCESS_KEY` — 🔴 같은 페이지에서 한 번만 표시. 분실 시 재발급
- `R2_BUCKET` — 버킷 이름 (Cloudflare 대시보드에서 미리 생성)
- `R2_PUBLIC_BASE_URL` — 옵션. 공개 도메인 연결 시. 없으면 presigned GET URL 만 사용

#### 권장 R2 셋업

1. Cloudflare 대시보드 > R2 > Create bucket: `terra-clips`
2. **버킷 비공개 유지** (Public Access OFF). 모든 접근은 presigned URL.
3. Manage R2 API Tokens > Create API token
   - Permissions: Object Read & Write
   - Specify bucket: `terra-clips` (보안 위해 한정)
4. 토큰 + Access Key ID + Secret 발급 → `.env` 에 기입
5. Lifecycle rule 추가 (Stage F2):
   - Prefix: `clips/`
   - Action: Delete after 30 days

## .env 생성 가이드 (Lightsail)

```bash
# 1. 템플릿 복사
cp .env.example .env

# 2. Supabase 키 입력
vim .env
# SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DEV_USER_ID 입력

# 3. MQTT 브리지 비번 생성 (강력)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 4. Mosquitto 에도 같은 비번 등록
sudo mosquitto_passwd /etc/mosquitto/passwd terra-bridge
# 위 토큰 붙여넣기

# 5. 권한
chmod 600 .env
```

## 흔한 실수

- `.env` 를 git 에 올림 → `.gitignore` 확인
- `AUTH_MODE=dev` 로 프로덕션 배포 → DEV_USER_ID 로 누구나 통과
- service_role key 를 앱에 임베드 → RLS 전부 무력화
- `MQTT_USE_TLS=false` 로 외부 노출 → 비번 평문 전송
- R2 bucket 을 Public 으로 설정 → presigned URL 의미 무력화 (영상 무단 접근 가능)
- R2 API 토큰 발급 시 "All buckets" 선택 → 권한 과다 (terra-clips 만 한정 권장)
