# AGENTS.md (모든 AI 에이전트용)

## 너 누구냐?

이 레포: **terra-server** — ESP32 사육장 IoT 백엔드 (Python 3.12 + FastAPI + paho-mqtt + Supabase)

한 줄: "ESP32 → MQTTS → Mosquitto → 브리지 → Supabase ← 앱(Realtime)"

## 에이전트별 출발점

| 에이전트 | 읽을 파일 |
|----------|---------|
| **Claude (Claude Code)** | `CLAUDE.md` 자동 로드 |
| Codex / ChatGPT | 이 파일 + `docs/ARCHITECTURE.md` |
| Cursor / Windsurf | 이 파일 + `docs/ARCHITECTURE.md` + `docs/DATABASE.md` |

## 필수 맥락

- 호스팅: **AWS Lightsail VPS (Ubuntu 22.04, $3.50)**, 서울 리전
- 브로커: Mosquitto 2.x (TLS, 8883)
- DB: Supabase (Postgres + Auth + RLS + Realtime), **신규 프로젝트** (petcam-lab 과 별개)
- 펌웨어 레포: `~/project/esp32/NimBLE_Connection` (별도 git 레포)
- 참조 레포: `~/project/petcam-lab` (FastAPI/Supabase 패턴 차용)

## 핵심 금지

1. **기억으로 단정 금지** — API/파일 경로는 반드시 `Read` 로 확인
2. **`pip install` 금지** — `uv add` 만
3. **service_role 키로 INSERT 시 `owner_id` 명시 필터 누락 금지**
4. **MQTT command publish 시 `retain=True` 금지** (replay 위험)
5. **비밀값 커밋 금지** (SUPABASE_SERVICE_ROLE_KEY, MQTT password 등)
6. **파괴적 git 명령 자동 실행 금지** (`reset --hard`, `--force` 등)
7. **디바이스/사용자 데이터 임의 생성 금지** — 테스트는 fixture

## 현재 단계 (Stage)

- Stage A: MQTT 브리지 기본 동작 (telemetry → DB 저장) — **진행 중**
- Stage B: 디바이스 페어링 (BLE + JWT)
- Stage C: 명령 디스패치 (commands → MQTT publish)
- Stage D: 알림 (alerts INSERT + 푸시)
- Stage E: 시계열 다운샘플 (telemetry → telemetry_1m, pg_cron)

각 스테이지 상세는 `specs/` 참고.
