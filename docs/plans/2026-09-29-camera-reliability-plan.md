# 카메라·예약 안정성 개선 — 실행 계획

> 진행 상태는 설계 §7 이 SOT (2026-09-29: PR1~PR6 → #7~#12 제출, 추가 #13, 머지 가이드 §8).

> 설계: [`specs/camera-reliability-2026-09.md`](../../specs/camera-reliability-2026-09.md) · 작성 2026-09-29 · 실행 레포 `/Users/baek/terra-server-camera-reliability` (worktree, 브랜치 `docs/camera-reliability-plan`)

## 0. 착수 전 (필수)
1. `git -C /Users/baek/terra-server-camera-reliability rev-parse HEAD` 가 인계서 SHA와 같은지 확인.
2. `git fetch origin && git log --oneline HEAD..origin/main` — main에 새 커밋이 있으면 설계의 코드 참조(≈줄번호)를 재확인. 충돌하는 변경(특히 `mqtt/handlers.py`, `dispatcher.py`, `schedule_runner.py`, `routers/webrtc.py`)이 있으면 설계 §1 해당 항목 먼저 갱신.
3. `uv sync && uv run pytest -q` 기준선 기록(09-29 main 346 passed).
4. 설계 §6 owner 결정 항목을 owner에게 **한 번에** 질문(AskUserQuestion). 결정 전엔 결정 무관한 PR부터 진행.
5. 이 worktree 브랜치는 **계획 문서 전용**. 구현은 항목별로 `origin/main`에서 새 브랜치를 딴다(`git switch -c feat/camera-health-events origin/main` 등). upstream은 자기 브랜치로만 설정(main 추적 금지).

## 1. 진행 순서 (owner 결정 의존도 낮은 것 먼저)
| 순서 | PR | 결정 의존 | 브랜치 예시 |
|---|---|---|---|
| 1 | P5-② 중복 메타 등록 idempotent | 없음 | `fix/clip-meta-idempotent` |
| 2 | P1 진단값 이력 | §6-1 | `feat/camera-health-events` |
| 3 | P2-(a) on/off 재전달 | §6-2 일부 | `feat/command-redeliver-onoff` |
| 4 | P2-(b) 상태 재조정 | §6-2 | `feat/actuator-reconcile` |
| 5 | P3 부분 멈춤 감지·알림 | §6-3, P1 | `feat/camera-degraded-alert` |
| 6 | P4 라이브 보호 | §6-4 | `feat/webrtc-offer-guard` |

## 2. 항목별 작업 (TDD — 실패 테스트 먼저)

### PR1 — P5-② clip 메타 idempotent
- [ ] `routers/clips.py` 메타 등록(≈:338~:388) 읽고 insert 경로 확인
- [ ] 테스트: 같은 clip_id·같은 r2_key 재전송 → 200(기존 행 반환), 같은 id·다른 key → 409
- [ ] 구현(PK 충돌만 좁게 처리, 광범위 except 금지)
- [ ] `docs/FIRMWARE_INTEGRATION.md`/`API.md`에 재전송 계약 한 줄

### PR2 — P1 `camera_health_events`
- [ ] 운영 DB 읽기로 `cameras.clip_stats` 실제 형태 샘플 3대 확인(현장 A/B/C 포함)
- [ ] migration SQL(`migrations/2026-09-xx_camera_health_events.sql`): 테이블 + 인덱스(camera_id, at desc) + RLS(owner 읽기 전용; 소유 판정 = cameras.owner_id)
- [ ] `tests/test_migration_coverage.py` 통과
- [ ] 핸들러: 직전 `uptime_s`·`reset` 메모리 캐시(카메라별) → 감소/변경 시 reset 행, N분마다 snapshot 행. **heartbeat UPDATE와 분리**(이벤트 insert 실패가 heartbeat를 막지 않게, 로그만)
- [ ] 브리지 재시작 직후 첫 값은 reset으로 오판하지 않기(`schedule_restore.note_uptime` 패턴 참고)
- [ ] 테스트: uptime 감소→reset 1행, 같은 부팅 반복→0행, snapshot 주기, insert 예외 시 heartbeat 정상
- [ ] PR 본문: migration 적용 요청(gwanhun), 보존 정리 방식 제안

### PR3 — P2-(a) on/off 재전달
- [ ] `mqtt/dispatcher.py` 상태 전이(:130 no_ack, :192 expired, :212 unknown_device) 재확인
- [ ] 설계: 재전달 대상 action 화이트리스트(`*_on/*_off`, relay는 §6-2), 유예 5분, 원 명령 id 연결(`source_id`/`reason` 활용 여부 확인)
- [ ] 테스트: 오프라인 중 fan_off → 복귀 후 1회 재발행 / 유예 초과 시 포기 / mist 재발행 안 함 / 중복 재발행 없음
- [ ] 구현

### PR4 — P2-(b) 상태 재조정
- [ ] `schedule_restore.py` 계산 로직(`compute_prev_run`) 재사용 범위 확인
- [ ] migration: commands `source` CHECK에 `'reconcile'` 추가(restore 추가 migration 패턴 참고)
- [ ] 테스트: 예약상 OFF 구간인데 telemetry fan2=ON → 교정 1회 / 이미 일치 → 0 / 사용자 수동 조작 규칙(§6-2) / 가드(skip_when_*) 적용
- [ ] 구현(주기·레이트 제한 포함)

### PR5 — P3 부분 멈춤 감지·알림
- [ ] P1 데이터 소스 사용. 판정 규칙: reset ∈ {PANIC, WDT, INT_WDT, TASK_WDT, SW:rtc_loop_stall, SW:upload_stuck} 또는 (`up_fail` 증가 & `up_ok` 정체 & `last_rec_s` 증가) — `last_rec_s` 단독 금지
- [ ] 중복 억제(카메라당 쿨다운), `alerts` kind 확장 여부 확인
- [ ] 푸시 이벤트 타입 추가 + 앱 전달 문서 초안(`docs/APP_CAMERA_DEGRADED_ALERT_2026-xx.md`)
- [ ] 재생 테스트: 현장 A/B/C 09-29 값으로 A PANIC·B stall → alert, C 정상 구간 → 0
- [ ] 자동 재부팅은 플래그 기본 off

### PR6 — P4 라이브 보호
- [ ] `routers/webrtc.py`/`webrtc_signaling.py` 오퍼·close 흐름 재확인, 두 시청자 session 처리
- [ ] 카메라별 오퍼 rate limit(429+Retry-After), 동시 세션 상한, 최대 시청 시간 — 저장소(메모리 vs DB) 결정: 서버 프로세스 1개면 메모리로 시작(YAGNI)
- [ ] 테스트: 상한 초과 429 / 정상 단일 시청 회귀 없음 / close 후 재시청 가능
- [ ] `docs/APP_WEBRTC.md` 개정(429 처리·재연결 백오프 권장)

## 3. 매 PR 공통
- [ ] `uv run pytest -q` 기준선 대비 실패 0
- [ ] 스키마 쓰는 코드는 운영 DB 컬럼 실측
- [ ] 커밋·push·PR 생성은 owner 승인 후(`gh pr create`, 본문 끝 `🤖 Generated with Claude Code`)
- [ ] PR 본문: 배경(설계 §0 수치), 변경, 테스트, gwanhun 요청(배포·migration 적용)
- [ ] 설계 §7 체크박스 갱신

## 4. 하지 말 것
- 현장 테스트 기기(A/B/C, tester01)에 명령·재부팅·예약 변경 — 10-03 00:00 KST까지 읽기만.
- main 직접 push, 운영 migration 적용, 운영 배포.
- 로컬 `.env`로 서버 실행해 production 데이터 생성.
