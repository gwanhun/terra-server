# 카메라·예약 안정성 개선 (2026-09 베타 멈춤 대응) — 설계

> 상태: 🟢 구현 중 — PR #7~#11 제출(2026-09-29), P4 owner 결정 대기 · 작성 2026-09-29 · 근거 데이터: petcam-lab 세션 분석 + 현장 A/B/C 테스트(`petcam-lab/experiments/camera-hang-ab-2026-09/`)
> 코드 참조는 **main `de9af0a` 기준**. 착수 전 반드시 재확인(다른 세션·gwanhun이 main에 직접 커밋함).

## 0. 배경 — 무엇을 봤나

베타 카메라(esp32-p4, `fb2-p4 0.1.0`)가 **하트비트는 살아있는데 녹화·라이브가 멈추고, 사람이 재부팅해야 회복**되는 현상이 9/23~ 반복됐다. 서버 데이터로 확인한 사실:

| 관찰 | 수치 |
|---|---|
| 멈추기 직전 업로드 재시도 적체 | 멈춤 8건 중 7건. 영상이 찍힌 뒤 수십 분~수 시간 늦게 등록 |
| 밀린 영상 대기 중에도 새 영상은 정상 업로드 | 밀린 804건 중 73% (네트워크 자체는 살아있음) |
| 베타 카메라 5분+ 지연 비율 | 56~70%, 최대 지연 47~1,029분 |
| 카메라 `clip_stats.last_err` | `stage 2`(영상 PUT) · `http 0` · `err 28674`(=0x7002 `ESP_ERR_HTTP_CONNECT`) — **연결 실패**, 403(URL 만료) 아님 |
| 현장 테스트 A(신규FW 0.2.0) | 09-29 15:00 `sys.reset=PANIC` 자동 재부팅, 부팅 후 up_ok 24 / up_fail 84 |
| 현장 테스트 B(신규FW 0.2.0) | 3시간 연속 라이브 재연결(100회+) 중 15:46~16:48 영상 없음 → 16:49 `sys.reset=SW:rtc_loop_stall` 자가 재시작 |
| 현장 테스트 C(기존FW 0.1.0) | 5분+ 지연 58~67%, 최대 68분. 12:14 `SW:rotate` 재시작 |
| 예약 명령 누락 | 09-29 07:30~10:00 3개 사육장 8건(`unknown_device`/`expired`), 08:10 B·C 동시 expired. B `fan2_off` 거부로 냉각팬 1시간 초과 가동, B 10:00 분무 누락 |
| 동시 시청 | 폰+시뮬레이터 동시 시청 시 B가 40초 stalled→unresponsive |

→ 카메라 멈춤의 근본 원인은 **펌웨어**(업로드 재시도 처리·라이브 처리) 쪽이 유력하다. 하지만 **서버가 ① 그 사실을 기록하지 못하고, ② 감지·알림하지 못하고, ③ 예약 명령을 잃어버리고, ④ 라이브 요청 폭주를 막지 못하는** 문제는 서버에서 고칠 수 있다.

## 1. 개선 항목 (우선순위 순)

### P1. 카메라 진단값 이력 저장 — 가장 시급
- **현재:** `mqtt/handlers.py` `handle_telemetry` 카메라 분기 → `_write_heartbeat`(≈:402)가 `clip_stats`(= `rec/up_ok/up_fail/sd_*/last_rec_s/last_err/sys{uptime_s,reset,heap,rssi}`)를 **cameras 행에 최신값으로 덮어씀.** 이력 없음. `sys`는 `clips` dict 가 있을 때만 저장(≈:677-679).
- **문제:** A의 PANIC, B의 자가 재시작을 우연히 조회해서 알았다. 베타 카메라들의 재시작 이력·업로드 실패 추세는 전부 유실.
- **설계안:**
  - 새 테이블 `camera_health_events`(append-only): `camera_id`, `at`, `kind`(`reset`|`snapshot`), `uptime_s`, `reset`, `up_ok`, `up_fail`, `sd_backlog`, `last_rec_s`, `last_err` jsonb, `heap`, `rssi`, `fw`.
  - **재시작 이벤트:** 직전 저장값 대비 `uptime_s`가 줄면(또는 `reset` 문자열이 바뀌면) `kind=reset` 1행.
  - **스냅샷:** 카메라당 N분(제안 10분)마다 1행. 15초 하트비트 전부 저장 금지(18대×5,760/일 — petcam Supabase Disk IO 예산 사고 전례).
  - 보존 기간 제안 30일(owner 결정).
- **완료 조건:** 재시작 1회 → reset 행 1개, 10분 스냅샷, 기존 heartbeat UPDATE 경로 실패 시에도 이벤트 기록이 heartbeat를 깨지 않음(선택 필드를 필수 UPDATE에 묶었다가 heartbeat 전체가 죽은 9/17·9/21 사고 재발 금지).

### P2. 예약·명령 전달 신뢰성
- **현재 (`mqtt/dispatcher.py`, `command_service.py`, `schedule_runner.py`):**
  - `DEFAULT_CMD_TTL_SEC = 10`. 기기가 오프라인이어도 `bridge.publish_command`는 로컬 paho 성공이면 `sent` → 30초 뒤 `no_ack`, **재시도 없이 버림.** handlers 캐시에 없으면 `rejected/unknown_device`.
  - `schedule_runner`는 `next_run_at`을 먼저 넘기고 명령 1건만 넣고 결과를 안 봄(이중 분무 방지 의도).
  - 상태 재조정 없음. `schedule_restore.py`는 **재부팅 감지 시 ON만** 복원(OFF 복원 없음).
- **설계안:**
  - **(a) on/off 계열 재전달:** `*_on/*_off`(led/fan/fan2/relay 제외 여부는 §6)가 `unknown_device`/`no_ack`/`expired`면 **짧은 유예(제안 5분) 동안 기기 재접속 시 재발행**. mist는 재발행 금지 유지(이중 분무 > 누락).
  - **(b) 상태 재조정 루프:** 기기 telemetry의 fan/fan2/led 실제 상태 vs 예약상 "지금 있어야 할 상태"(`compute_prev_run` 재사용 — `schedule_restore`와 같은 계산)를 주기 비교 → 어긋나면 교정 명령 1회(`source='reconcile'`, CHECK 제약 migration 필요). 사용자 수동 조작과 충돌 규칙은 §6.
  - **(c) 원인 조사:** 08:10 B·C 동시 `expired` — 브리지/브로커 단 연결 끊김 가능성. 운영 로그(gwanhun 서버)에서 그 시각 MQTT 재연결 여부 확인.
- **완료 조건:** 오프라인→복귀 시나리오 테스트에서 fan_off가 복귀 후 전달됨, 재조정 루프가 켜진 채 남은 팬을 1회 끔, mist는 재발행 안 됨.

### P3. 카메라 "부분 멈춤" 감지 + 알림 (+ 선택: 자동 재부팅)
- **현재:** `offline_monitor.py`는 기기만 alert, 카메라는 `is_online=false`만. 푸시는 `device.action.*`만(`push_events.py`). 부분 멈춤 감지 없음(handlers의 warning 로그뿐, ≈:508-521). 카메라 원격 재부팅 API는 있음(`POST /cameras/{id}/reboot`, `routers/cameras.py` ≈:459-493, fire-and-forget).
- **설계안:** 하트비트 기반 판정(서버가 이미 받는 값만 사용)
  - `last_rec_s`가 크면서 같은 기간 `up_fail`만 증가, 또는 `sys.reset ∈ {PANIC, WDT, INT_WDT, TASK_WDT, SW:rtc_loop_stall, SW:upload_stuck}` 발생 → `alerts` 행(kind `camera_degraded`) + 사용자 푸시(새 이벤트 타입, 앱 계약 필요).
  - ⚠️ `last_rec_s`만으론 "게코가 안 움직임"과 구분 불가(현장에서 실측됨) → 단독 조건 금지, `up_fail` 증가·reset 사유와 결합.
  - 자동 재부팅은 **기본 꺼둠**, owner 결정 후 옵트인(§6).
- **완료 조건:** PANIC/stall reset 이벤트 → alert 1건(중복 억제), 정상 카메라 오탐 0(현장 A/B/C 이력으로 재생 테스트).

### P4. 라이브(WebRTC) 요청 보호
- **현재 (`routers/webrtc.py` ≈:250-326, `webrtc_signaling.py`):** 오퍼당 최대 3회×7초. 카메라당 동시 시청 제한·반복 오퍼 rate limit 없음. `stream_until=now+5분` 기록만 하고 어디서도 적용 안 함. 두 시청자는 서로 다른 session_id로 카메라에 직행, 첫 `/close`가 `stream_mode`를 둘 다 지움.
- **관찰:** B에 3시간 동안 오퍼 100회+ → 매 ~16초 stalled → 영상 없음 → 자가 재시작. 동시 시청 시 40초 먹통.
- **설계안:** 카메라별 오퍼 rate limit(예: 1분 N회, 초과 시 429 + `Retry-After`), 동시 세션 상한(1~2, owner 결정), 최대 시청 시간 강제(`stream_until` 실제 적용 또는 세션 재발급 요구). 앱 쪽 재연결 백오프는 앱 전달 문서로.
- **완료 조건:** 상한 초과 오퍼 429, 기존 단일 시청 흐름 회귀 없음(`docs/APP_WEBRTC.md` 계약 유지 또는 개정 문서화).

### P5. 업로드 경로 견고화 — 펌웨어 확인 후
- **현재 (`routers/clips.py` ≈:289, :338, `r2_client.py` :50):** PUT URL TTL **300초**, clip_id는 upload-url 발급 시 서버가 `uuid4`로 생성, 메타 등록은 업로드 뒤, **중복 id insert → 500(미처리 APIError)**.
- **가설(미확정):** 펌웨어가 재시도 때 예전 URL을 재사용하면 5분 뒤 계속 403. 단 **관측된 last_err는 http 0(연결 실패)** 이라 현재 증거는 이 가설을 지지하지 않음.
- **설계안:** ① 펌웨어 담당에게 "재시도 시 upload-url 재발급 여부 / 메타 재전송 여부" 확인 ② 중복 메타 등록은 idempotent(같은 id+같은 r2_key면 200) ③ 필요 시 TTL 연장 또는 "같은 clip_id로 URL 재발급" API.
- **완료 조건:** ②는 즉시 가능(테스트 포함). ①③은 펌웨어 답 후 결정.

## 2. 범위 밖 (이 작업에서 하지 않음)
- 펌웨어 수정(재시도 로직·워치독) — 펌웨어 담당.
- 운영 배포, 운영 DB migration 적용, `MIGRATIONS_APPLIED.md` 갱신 — **gwanhun 몫.** 우리는 코드 + SQL 파일 + PR 본문 요청까지.
- 앱 UI 변경 — 앱 전달 문서(`docs/APP_*.md` 형식)로 요청만.

## 3. 제약 (이 레포 규칙)
- **main 직접 push 금지.** 항목별 브랜치(`feat/…`, `fix/…`) + PR. PR 올리기 전 커밋·push는 owner 승인.
- service_role 쿼리는 `owner_id` 명시 필터. MQTT command `retain=False`. 기기/사용자 데이터 임의 생성 금지(fixture만).
- **Supabase는 petcam-lab과 같은 프로젝트** → migration·트리거·RLS 변경은 양쪽에 영향. 로컬 `.env`로 서버를 띄우면 **production DB·버킷에 직접 씀**(pytest는 conftest monkeypatch로 안전).
- terra 테스트는 Supabase MagicMock → 컬럼 부재·필터 누락을 못 잡음. **스키마 의존 변경은 운영 DB 읽기로 컬럼 실측 + `tests/test_migration_coverage.py` 가드.**
- 선택 필드를 필수 heartbeat UPDATE에 묶지 않기(9/17 image_state·9/21 hw_id 사고).
- 기준선: `uv run pytest -q` (09-29 main 기준 346 passed — 착수 시 재측정).

## 4. 검증 데이터
- 현장 테스트 카메라(tester01): A `0bf9f0e1-dcb8-4848-ac8b-1957abc2e189` / B `f36777f5-14db-4cbb-b905-a92e66b1e2d2` / C `99c48bc6-240c-434d-aae8-23fca670a14a`, 사육장 보드 A `e146a8dd…` B `a00b49e0…` C `f0749d91…`. 테스트는 **10-03 00:00 KST까지 진행 중** — 이 기기들에 명령·재부팅·예약 변경 금지(읽기만).
- 명령 실패 실례: 09-29 `commands` source=schedule, status≠acked (B 02:10 fan2_off unknown_device, 08:10 B·C expired, B 10:00 mist expired 등).
- 라이브 폭주 실례: `webrtc_connect_logs` camera B, 09-29 13:15~17:20 platform=ios.

## 5. PR 분할
| PR | 항목 | 스키마 변경 |
|---|---|---|
| 1 | P1 진단값 이력 | 새 테이블 migration |
| 2 | P5-② 중복 메타 idempotent | 없음 |
| 3 | P2-(a) on/off 재전달 | 없음(또는 컬럼 1개) |
| 4 | P2-(b) 상태 재조정 | commands source CHECK 확장 |
| 5 | P3 부분 멈춤 감지·알림 | alerts kind 확장 가능 |
| 6 | P4 라이브 보호 | 없음(메모리/DB 카운터 결정) |

## 6. owner 결정 (2026-09-29)
1. P1 — **스냅샷 10분 · 보존 30일** ✅
2. P2 — **relay(펌프) 제외, 수동 조작 존중**(예약 구간 중 수동 조작은 그 구간 끝까지 안 덮음) ✅
3. P3 — **alert 행만, 푸시 보류, 자동 재부팅 없음** ✅
4. P4 — 동시 시청 상한·최대 시청 시간: **미정**("고객 사용 패턴을 모름, 더 오래 볼 수도 있음"). rate limit 임계값도 아래 실측 때문에 결정 대기
5. P5 — 펌웨어 질문은 **이관훈님 Slack DM** — 09-29 발송 완료(재시도 시 upload-url 재발급·메타 재전송·201 vs 2xx 판정·err 28674/백오프)

### P4 실측 (webrtc_connect_logs 09-22~29, 1,719행, 28대, 읽기만)
| 창 | 현장 B(f36777f5) | 나머지 카메라 최대 |
|---|---|---|
| 60초 | 6 | 8 |
| 10분 | 20 | 23 |
| 1시간 | **81** | 38 |
→ 분·10분 단위 rate limit 은 B 폭주를 못 막고 정상 사용만 막는다. 구분되는 건 1시간 창뿐.
B 09-29 13:15~17:20: 248건(streaming 89 / stalled 88 / no_video 58), 간격 p50 63초 — 앱의 stalled→재연결 루프가 본질.

## 7. 체크리스트 (진행 상태 SOT)
> PR 은 제출 = 체크. 머지·migration 적용·배포는 gwanhun 몫이라 별도 표기.

- [x] P1 설계 결정(§6-1) → [#8](https://github.com/gwanhun/terra-server/pull/8) `camera_health_events` (migration 적용 요청)
- [x] P5-② idempotent → [#7](https://github.com/gwanhun/terra-server/pull/7)
- [x] P2-(a) → [#9](https://github.com/gwanhun/terra-server/pull/9) 재전달 (migration 없음, restore source 재사용)
- [x] P2-(b) → [#10](https://github.com/gwanhun/terra-server/pull/10) 재조정 (commands source CHECK migration 선적용 필요)
- [x] P3 → [#11](https://github.com/gwanhun/terra-server/pull/11) `camera_alerts` (migration 적용 요청)
- [ ] P4 → PR6 — owner 결정 대기(§6-4 실측)
- [ ] P5-①③ 펌웨어 답변 반영 (09-29 DM 발송, 답 대기)
- [ ] 앱 전달 문서(P3 푸시 — 보류 결정, P4 429/재연결 백오프 — P4 결정 후)
- [ ] #8·#11 적용 후 쌓인 이력으로 P3 임계값(30분·3회) 재검토
- 머지 충돌 주의: #9↔#10 (handlers 기기 분기·reset_device_cache), #8↔#11 (handlers 카메라 분기)
