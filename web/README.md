# terra-server / web — 로컬 테스트 콘솔

단일 HTML 파일. Supabase Auth + 백엔드 API 호출 검증용.

> sample_project 의 ESP32 펌웨어 웹페이지 패턴 차용 (Vanilla JS, 인라인 CSS, dark theme).
> 빌드 도구 없음, npm 의존성 없음. 그냥 정적 파일.

## 띄우기

```bash
# 프로젝트 루트에서
python3 -m http.server -d web 5500

# 브라우저로 http://localhost:5500/ 열기
```

또는 `npx serve web -p 5500` 같은 다른 정적 서버도 OK.

## 첫 설정

### 1) 백엔드 `.env` — `SUPABASE_PUBLISHABLE_KEY` 추가 (한 번만)

Supabase 대시보드 > Settings > API:
- 새 UI: **Publishable keys** 섹션의 키
- 구 UI: **Project API keys** 의 `anon public` 키

복사 후:
```bash
# .env 에 한 줄 추가
SUPABASE_PUBLISHABLE_KEY=eyJ...  # 또는 sb_publishable_xxx
```

> 클라이언트 공개 OK (RLS 가 보호). **`SUPABASE_SERVICE_ROLE_KEY` / Secret keys 와 혼동 금지** — 그건 백엔드 전용.

백엔드 재시작:
```bash
# 로컬
uv run uvicorn backend.main:app --reload
# Lightsail
sudo systemctl restart terra-api
```

### 2) 브라우저에서 — **API Base 만 입력**

페이지 로드 시 입력 1개:

| 항목 | 예시 |
|------|------|
| API Base | `http://localhost:8000` (로컬 uvicorn) 또는 `https://api.terra-server.uk` (Lightsail) |

→ 그러면 페이지가 `GET {API_BASE}/web-config` 호출해서 `SUPABASE_URL` + `anon key` **자동으로 받아옴**.

→ localStorage 에 apiBase 만 저장. 초기화는 "초기화" 버튼.

## 백엔드 CORS 설정 필수

`.env` 의 `APP_ORIGINS` 에 로컬 웹 서버 주소 추가:

```bash
APP_ORIGINS=http://localhost:5500,https://app.example.com
```

이후 백엔드 재시작:
```bash
# 로컬
uv run uvicorn backend.main:app --reload

# Lightsail
sudo systemctl restart terra-api
```

## 백엔드 인증 모드

| AUTH_MODE | 동작 |
|-----------|------|
| `prod` | 로그인 후 JWT 검증, **본인 데이터만 보임** (정상 흐름) |
| `dev` | JWT 무시, `DEV_USER_ID` 데이터만 보임 (회원가입/로그인 UI 동작은 함, 단 본인 데이터 X) |

→ 회원가입/로그인 검증하려면 **`AUTH_MODE=prod`** 권장.

## 기능

- 🔑 **회원가입 / 로그인 / 로그아웃** (Supabase Auth, email/password)
- 🏠 **사육장** (`/enclosures`): 생성·목록·삭제
- 📟 **디바이스** (`/devices`): 페어링·목록·삭제 (MQTT 토큰 1회 노출)
- 📷 **카메라** (`/cameras`): 페어링·목록·삭제 (Camera 토큰 1회 노출)
- 📜 최근 API 응답 로그

## 안 들어간 것 (의도)

- 카메라 클립 재생 (Stage F2 R2 검증 후 추가)
- 텔레메트리 실시간 차트 (Realtime 구독, 별도 추가)
- 명령 발행 (Stage C 백엔드 완성 후)
- snapshot/WebRTC 라이브 (Stage G)
