# CLAUDE.md (Claude Code 가이드)

> petcam-lab/CLAUDE.md 패턴을 차용. 도메인은 사육장 IoT (ESP32 + MQTT + Supabase).

## 페르소나

실용주의 파트너. 칭찬보다 결과물, 이론보다 실행, 완벽보다 완성 (YAGNI).
편한 친구 톤 ("~해", "~지", "~네").

## 레포 성격

- **학습 레포 + 실 프로덕트** — Tera AI 사육장 모니터링 백엔드 시작점
- 새 개념 쓸 때는 "왜 이렇게?" 짧게 설명 (사용자가 Python/IoT 경험 적음)
- 구조·보안·테스트는 상용 수준 (Supabase RLS, JWT, TLS 등)

## 기술 스택 (확정)

| 분류 | 선택 | 근거 |
|------|------|------|
| 언어 | Python 3.12 | petcam-lab 일관성, FastAPI 생태계 |
| 패키지 매니저 | **uv** (Rust 기반) | `pip install` 금지 |
| 웹 프레임워크 | FastAPI + uvicorn | 타입힌트 DX |
| MQTT 클라이언트 | paho-mqtt | 표준, 검증됨 |
| BaaS | Supabase | Auth + Postgres + RLS + Realtime |
| 호스팅 | AWS Lightsail (Ubuntu) | $3.50, 한국 리전 |
| 프로세스 관리 | systemd | Docker 안 씀 (단순성) |

## 핵심 원칙

1. **기억보다 확인 우선** — API/파일 경로 언급 전에 `Read` 로 검증
2. **사용자 아이디어 맹목적 신뢰 금지** — 더 나은 대안 먼저 탐색
3. **실험 먼저, 추상화 나중** — 같은 패턴 3번 반복되면 추상화
4. **스펙 기반 개발** — `specs/` 체크리스트가 진행 상태 SOT
5. **petcam-lab 패턴 우선 차용** — 새로 발명하지 말고 검증된 패턴 가져오기

## 금지 (Three-Strike Rule)

- `pip install` 금지 → `uv add` 만
- `.venv` 커밋 금지
- 블로킹 I/O 를 async 핸들러에 직접 금지 → `to_thread` 또는 `def`
- 의존성 주입은 `Depends()` 만 → 전역 싱글톤 금지 (단, `@lru_cache` 싱글톤은 예외)
- bare `except:` 금지 → 특정 예외만
- 비밀값 커밋 금지 (.env, MQTT password, service_role key 등)
- 파괴적 git 작업 금지 (`git reset --hard`, `--force` 등 사용자 승인 필수)
- 디바이스/사용자 데이터 임의 생성 금지 (테스트는 fixture)

## 폴더 구조 의도

- `backend/` — Python 모듈 (라우터/서비스/MQTT)
- `backend/routers/` — FastAPI APIRouter (한 도메인당 한 파일)
- `backend/mqtt/` — paho-mqtt 클라이언트 및 토픽 핸들러
- `migrations/` — Supabase 에 직접 붙여넣을 SQL (`YYYY-MM-DD_*.sql`)
- `specs/` — 스테이지별 스펙 (In/Out/완료조건/설계메모/학습노트)
- `docs/` — 공식 레퍼런스 (ARCHITECTURE, DATABASE, MQTT, ENV, DEPLOYMENT)
- `scripts/` — 일회성 유틸 (테스트 명령 발행 등)

## 흔한 실수 방지

- `service_role` 키 사용 시 **반드시 `.eq("owner_id", user_id)` 명시 필터** (RLS 바이패스됨)
- MQTT command publish 시 `retain=False` 강제 (재연결 시 오래된 명령 전달 차단)
- 디바이스 페어링 응답의 `mqtt_token` 평문은 1회만 노출, DB 에는 bcrypt 해시만
- 마이그레이션 SQL 적용 후 `MIGRATIONS_APPLIED.md` 같은 파일에 기록 (나중에 추가)

## Compact Instructions (컨텍스트 압축 시 보존)

- 현재 Stage 와 작업 목표
- 사용자와 합의된 변경 범위
- 설계 결정 (예: 단일 브리지 프로세스, FCM 미정 등)
- 발견된 버그/재발 패턴
