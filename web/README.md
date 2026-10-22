# terra-server / web — 로컬 테스트 콘솔

정적 HTML 3 페이지 + 공용 JS/CSS. Supabase Auth + 백엔드 API 호출 검증용.

| 파일 | 역할 |
|------|------|
| `index.html` | 메인 콘솔 — 로그인한 계정의 사육장·기기·카메라·예약·설정·텔레메트리·알림. 상단 네비와 로그인 패널의 버튼으로 아래 두 페이지로 이동 |
| `beta.html` | 🎫 베타 테스터 — 테스트 계정별 기기·카메라 조회·제어(테스트/일괄)·재부팅·OTA·해제·삭제. 2026-10-21 index 의 패널에서 분리 |
| `live.html` | 🎥 라이브 대시보드 — 베타 카메라 라이브를 타일 그리드로 동시 시청(계정별 JWT 로 WebRTC 각각). beta 의 [▶ 라이브]는 `live.html?cam=<uuid>` 로 그 카메라만 바로 연결 |
| `shared.js` | esc/toast/OTA 모달/베타 계정 인증(`regSession`, 토큰 1h 캐시, signOut scope local)/상단 네비/테스터 이름(`BETA_NAMES_DEFAULT`, textarea 의 "이메일 이름" 으로 덮어씀) |
| `shared.css` | 공용 스타일(다크 테마, 네비, 칩, 카드, 모달, 모바일) |

**세 페이지 모두 같은 정책**(2026-10-22): Supabase 로그인(같은 세션) + `GET /console/access` admin. `index.html` 도 로그인 뒤 같은 검사를 통과해야 대시보드가 열린다. 서버 `.env` 의 `CONSOLE_ADMIN_EMAILS` 에 운영자 이메일을 쉼표로 — 비어 있으면 로그인한 아무 계정이나 통과하니 운영에선 꼭 채울 것. 테스터 공통 비밀번호는 소스에 없고 ⚙️ 설정에 한 번 입력하면 브라우저(localStorage `terra-beta-pass`)가 기억한다.

세 페이지는 localStorage 를 공유한다: `terra-config`(apiBase), `terra-beta-emails`(베타 계정 목록), `terra-live-viewer-id`(라이브 "한 기기" 판정용 viewer_id).

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
- 📟 **디바이스** (`/devices`): 페어링·목록·삭제 (MQTT 토큰 1회 노출). `fw` 열(telemetry 보고 펌웨어 버전), `sys` 색 기준은 표 아래 범례 — 빨강=지금 조치(크래시·전원 부족 재부팅 24h 안, 내부 RAM 최대 블록 16K/여유 32K 미만 — P4 평시 60~128K/31~42K, RSSI −80 이하), 주황=관찰(자가 복구 재부팅, 약한 WiFi), 하루 넘게 돌면 재부팅 사유 색 없음
- 📷 **카메라** (`/cameras`): 페어링·목록·삭제 (Camera 토큰 1회 노출). 클립 셀 색: 빨강=업로드 고착·인코더 연속 실패·플래핑·스킵 20%+·SD fault 3+, 주황=스킵·업로드 실패·클립 유실(24시간 안)·SD 백로그 3+ (유실·백로그는 빨강 없음, owner 2026-10-21)
- 📱 **모바일**: ≤600px 에서 표가 카드(라벨 값)로 바뀜 — `enableTableLabels()`(shared.js) 가 th → td `data-label` 을 붙이고 shared.css 가 세운다
- 🎫 **베타 테스터** (`beta.html`): 계정별 등록 현황 조회·테스트·일괄 제어·재부팅·OTA·해제·삭제 (신규 등록은 앱에서 — 2026-10-08 콘솔 발급 버튼 제거)
- ⬆️ **선택 일괄 OTA** (`beta.html`): 카메라·기기 체크 → target(camera_p4/hub_p4/device_nano)별 릴리스 선택 → [1단계 prepare 모두](간격, 기본 15초) → 작업 표 8초 폴링(GET /firmware/jobs/{id}, 그 계정 JWT) → [2단계 apply · ready 전부](5초 간격). 서버는 기기당 진행 중 1개·같은 버전 409 만 막고 전역 동시 제한은 없음(스펙 결정 6 "한 대씩"은 canary 권고) — 바이너리가 API 서버를 거치니 간격 유지
- 🎥 **라이브 대시보드** (`live.html`): 베타 카메라 동시 시청. 서버 규칙(15분·5분 쉼·한 기기, `docs/APP_LIVE_VIEW_LIMIT_2026-09-30.md`)을 타일에 표시 —
  409 live_in_use 는 "○○에서 시청 중" + [가져오기], 429 는 카운트다운. 끊긴 뒤 자동 재연결 안 함(재연결 루프 사고 방지). [전체 연결]은 2초 간격 순차
- 📈 **온도 추이** (`telemetry_30m`): 디바이스 행 `[📈추이]` — 24시간/7일/30일,
  평균선 + 그 30분의 최저~최고 밴드 + 목표 온도 범위 띠. 오프라인 구간은 선이 끊긴다.
- 📜 최근 API 응답 로그

## 안 들어간 것 (의도)

- 카메라 클립 재생 (Stage F2 R2 검증 후 추가)
- 습도 추이 차트 (온도 추이와 같은 경로 — `h_a_*` 로 바꾸면 되지만 필요해질 때)
- 명령 발행 (Stage C 백엔드 완성 후)
- 라이브 음성 (Stage G3 이후 — 카메라는 영상만)
