# 운영 카메라 재부팅 API Implementation Plan

> **구현 방식 (CAOF):** **트랙: Critical** — 새 인증 경로(서버 시크릿)·새 테이블·MQTT 브리지 수정·문서 5종, 남의 카메라를 재부팅하는 기능. `/검수` 반영 완료(`reports/stage-m-ops-camera-reboot-review-final.md`) → 사용자 승인 → Implementer 에이전트(GATE 4). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** petcam-lab 감시 스크립트가 사용자 로그인 없이 서버 시크릿만으로 베타 카메라를 재부팅할 수 있는 `POST /ops/cameras/{camera_uuid}/reboot`를 만들고, 재부팅마다 기록을 남기며, 앱에는 알리지 않는다.

**Architecture:** 새 라우터 `backend/routers/ops.py`가 `X-Ops-Secret` 헤더를 `.env OPS_REBOOT_SECRET`과 상수 시간 비교로 인증한다. 대상 제한(beta·연결됨)·온라인·라이브·OTA·쿨다운을 차례로 검사하고, **기록 행을 먼저 INSERT한 뒤**(실패하면 발행 안 함 = fail-closed) 기존 `reboot_command()` + `MqttWebRTCSignaling().publish()`로 발행하고 결과를 UPDATE한다. MQTT 브리지의 카메라 ack 처리(`_ack_camera`)가 같은 msg_id 행에 ack 결과를 채운다.

**Tech Stack:** FastAPI(`def` 핸들러), supabase-py(service_role), paho-mqtt(기존 `MqttWebRTCSignaling`), pytest + `tests/fake_sb.py`.

**Spec:** `/Users/baek/petcam-lab/docs/requests/2026-10-20-terra-server-ops-camera-reboot.md` (요청 6)

## 분석 요약 (계획 전제)

| 항목 | 확인 결과 | 근거 |
|------|----------|------|
| 기존 재부팅 경로 | `POST /cameras/{id}/reboot`가 주인 JWT 요구, 발행 실패는 200 `published=false` | `backend/routers/cameras.py:613-648` |
| 발행 함수 | `publish()`는 None 반환, 실패 시 `WebRTCSignalingError`/`Timeout` 예외(최대 5초 대기) | `backend/webrtc_signaling.py:158-182` |
| `labeling_group` | petcam-lab 마이그레이션 컬럼. NULL=내부, `'beta'`만 허용. **"필터일 뿐 권한 아님"** — petcam 감시 스크립트가 직접 설정한다 | `petcam-lab/migrations/2026-09-22_labeling_camera_groups.sql:10-13` |
| 라이브 판정 | 요청서 규칙 `live_session_id` NOT NULL 또는 `stream_until > now()`(live_session 기준의 상위집합). 만료 세션은 `LiveSessionReaper`가 15초마다 지워 영구 차단 없음 | `backend/live_session.py:47,112,165-190` |
| OTA 진행 판정 | `ota_service.active_job_for(sb, uuid)` 기존 함수 | `backend/ota_service.py:135` |
| 카메라 ack | 브리지 `_ack_camera`가 로그 + `last_seen` 갱신 + OTA 라우팅. 브리지는 API와 **별도 systemd 유닛 `terra-bridge`** | `backend/mqtt/handlers.py:1086-1130`, `docs/DEPLOYMENT.md:146,174` |
| 프로세스 수 | uvicorn `--workers` 없음 = 단일 프로세스 → 프로세스 내 락으로 충분 | `docs/DEPLOYMENT.md:146` |
| **앱의 재시작 표시 (요청서 2-3 "확인 필요")** | **사용자가 누른 재시작에만 반응.** `RebootController._hold()`가 `request()` 이후에만 heartbeat를 구독. 다른 `sysHealthProvider` 사용처는 `isOnline`만 읽음. 누르지 않은 재부팅 토스트 없음 (검수자 독립 재확인 일치) | `tera-ai-flutter/lib/features/my_cage/presentation/sys_health_controllers.dart:160-235`, `domain/sys_health.dart:102` |
| 서버 푸시 | 카메라용 앱 푸시 없음 | `backend/push_events.py` |

→ **2-3은 "푸시 미발행"만으로 끝난다. `last_ops_reboot_at` 컬럼은 만들지 않는다.** 회신서에 근거를 적고 앱 담당 최종 확인만 요청한다.

## 가정

- 확인: 위 표 전부. 2차 베타 9대가 `reboot` 지원(요청서 1절).
- 미확인: 카메라 ack에 `action` 필드가 실리는지, msg_id를 그대로 돌려주는지 → ack 기록은 `action`에 의존하지 않고 **UUID로 파싱되는 msg_id**로만 매칭한다(Task 3).

## 선택지와 추천

| 결정 | 선택지 | 추천 |
|------|--------|------|
| 경로·헤더 | 요청서 그대로 `/ops/...` + `X-Ops-Secret` / `Authorization: Bearer` | **요청서 그대로.** 사용자 JWT와 섞이지 않는다 |
| 시크릿 미설정 | 401 / 503 | **401**(설정 상태 비노출). 경고 로그는 앱 시작 시 1회 |
| 기록·발행 순서 | 발행 → 기록 / **기록 → 발행 → 결과 UPDATE** | **기록 먼저(fail-closed).** INSERT 실패 → 발행 안 함, 503 `{"detail":"ops log unavailable"}`. 마이그레이션 전 배포·Supabase 순단에도 쿨다운이 안 뚫린다. ack가 INSERT보다 먼저 와 유실되는 경합도 없다 |
| 기록 대상 | 발행 시도만 / 건너뜀까지 | **발행 시도만.** 건너뜀은 petcam 슬랙에 남는다 |
| 쿨다운 기준 | 모든 시도 / 발행 실패 제외 | **`published`가 false가 아닌 행**(성공 + 진행 중). 발행 실패는 "다음 실행에서 재시도"(요청서) |
| `result` 컬럼 | 단일 `result` / `published` + `ack_result` + `acked_at` | **분리.** 발행과 ack는 채워지는 시점이 다르다. 소비자는 아직 없다 |
| OTA 중 재부팅 | 무시 / **409 `ota_active`** | **건너뜀(계약 추가).** 진행 중 OTA를 깨면 작업이 타임아웃까지 묶인다 |
| FK | `ON DELETE CASCADE` / FK 없음 | **FK 없음** — 카메라 행이 지워져도 리포트 기록 보존(`ota_jobs`와 같은 관례) |
| 검사 순서 | — | 401 → 404 → `not_beta` → `offline` → `live_active` → `ota_active` → (락) 쿨다운 429 → INSERT → 발행 → UPDATE |

## 실패 시나리오

1. **시크릿 유출 또는 감시 스크립트 버그** → 베타 카메라만, 카메라당 2시간 1회가 상한. `labeling_group`은 감시 스크립트가 직접 쓰는 값이라 스크립트가 엉뚱한 카메라를 beta로 찍으면 막지 못한다 — 잘못된 UUID만 막는다(회신서에 명시). 배포 전 `labeling_group='beta' AND unlinked_at IS NULL` 개수가 9인지 확인. 로테이션은 `.env` 교체 + API 재시작.
2. **매시 호출 버그** → 서버 쿨다운 429.
3. **동시 호출 2건** → 카메라별 `threading.Lock`이 쿨다운 조회~INSERT를 직렬화. 두 번째는 진행 중 행(`published=NULL`)을 보고 429.
4. **기록 INSERT 실패** → 발행하지 않음, 503. 재부팅도 쿨다운 우회도 없다.
5. **발행 후 결과 UPDATE 실패** → 행은 `published=NULL`로 남아 쿨다운에 계속 포함된다(안전 쪽). `logger.error`.
6. **라이브 판정과 발행 사이에 시청 시작** → 락 밖 수 ms 창. 감수.
7. **내부/고객 카메라 UUID** → 409 `not_beta`, 발행·기록 없음.

## 사용자가 정할 것 (owner)

1. ✅ 쿨다운 2시간 (owner 2026-10-20 승인). 한 밤 최대 2회는 petcam 스크립트 몫이라 서버는 강제하지 않는다.
2. ✅ 계약 추가 2건 승인 (owner 2026-10-20): 409 `ota_active`, 503 `ops log unavailable`.
3. `OPS_REBOOT_SECRET` 생성·서버 `.env` 반영·petcam 전달은 owner(값은 커밋·채팅 금지).
4. 베타 종료(2026-11-02 전후) 후 유지 여부 — 이번엔 안 정함.

## 검증 방법

- 자동: `uv run pytest -q` 전체 통과.
- 배포 후(사용자/owner): 요청서 4절 1~6 + 배포 전 beta 카메라 수 확인. "앱 화면 표시 없음"은 사용자만 확인 가능.

## Global Constraints

- 인증: 이 라우터에 `get_current_user_id`를 붙이지 않는다. 비교는 `hmac.compare_digest`. 서버 시크릿이 비었거나 미설정이면 헤더와 무관하게 401. 시크릿·헤더 값을 로그에 남기지 않는다.
- 대상: `labeling_group == 'beta'` 이고 `unlinked_at IS NULL`.
- 쿨다운: `OPS_REBOOT_COOLDOWN_SEC = 2 * 60 * 60`.
- 발행: `reboot_command()` + `MqttWebRTCSignaling().publish(camera_id_text, cmd)` 재사용. 펌웨어 변경 없음.
- 앱 알림: `push_events`·알림 outbox 호출 없음. `cameras`에 컬럼 추가 없음.
- 새 의존성 없음(`pip install` 금지).
- 건드리지 않는 것: `POST /cameras/{id}/reboot` 동작, `live_session.py`, `ota_service.py`(읽기 함수 호출만), 펌웨어, petcam-lab 레포, 앱 레포.

## Review Focus

1. 서버 시크릿 미설정 + 헤더 `""` → 401(빈 값끼리 일치 금지). → Task 2 `test_ops_reboot_401_when_server_secret_unset`.
2. `stream_until` 과거 + `live_session_id` NULL → 발행. 미래면 `live_active`. → Task 2 `test_ops_reboot_live_by_stream_until_only`.
3. 직전 행이 `published=false`뿐이면 쿨다운 없음, `published=None`(진행 중)이면 429. → Task 2 `test_ops_reboot_failed_publish_does_not_start_cooldown`, `test_ops_reboot_pending_row_blocks`.
4. 기록 INSERT 실패 → 발행 0회 + 503. → Task 2 `test_ops_reboot_insert_failure_fails_closed`.
5. 비-UUID msg_id·webrtc·OTA ack가 `camera_ops_reboots` UPDATE를 일으키지 않는다. → Task 3 `test_camera_ack_skips_non_ops_acks`.

---

### Task 1: `camera_ops_reboots` 마이그레이션 + FakeSB `gte`

**Files:**
- Create: `migrations/2026-10-20_camera_ops_reboots.sql`
- Modify: `tests/fake_sb.py` (`gte` 필터)
- Test: `tests/test_fake_sb_ext.py`

**Context:**
- Depends on: 없음
- Produces: 테이블 `camera_ops_reboots`, `FakeSB` 쿼리의 `.gte(col, val)`
- Must know: 운영 DB 적용은 사용자가 코드 배포 **전**에 한다. `gte`는 `lt`와 같은 ISO 문자열 비교 + None 가드(None 값 행은 불일치).
- Acceptance: `uv run pytest tests/test_fake_sb_ext.py -q` 통과.

- [ ] **Step 1: 마이그레이션 작성 (전문)**

```sql
-- 2026-10-20: 운영 카메라 재부팅 기록 (petcam-lab 요청 6, specs/stage-m-ops-camera-reboot.md)
-- POST /ops/cameras/{id}/reboot 발행 시도마다 1행 — 발행 전에 INSERT(published=NULL) 하고 결과로 UPDATE.
-- 쿨다운 판정·베타 리포트 집계·ack 확인용. FK 없음: 카메라 행이 지워져도 기록 보존(ota_jobs 관례).
-- RLS 켜고 정책 없음 = service_role 전용 (앱·anon 읽기 불가).
-- **코드 배포 전에 적용할 것** — 없으면 API 가 INSERT 실패로 503(재부팅 안 함). 적용 후 MIGRATIONS_APPLIED.md 기록.

CREATE TABLE IF NOT EXISTS public.camera_ops_reboots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id     UUID NOT NULL,                  -- cameras.id
    msg_id        UUID NOT NULL UNIQUE,           -- 발행한(하려던) reboot 명령 msg_id
    reason        TEXT NOT NULL,                  -- 예: night_rec_stall
    requested_by  TEXT NOT NULL,                  -- 예: beta-camera-watch
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published     BOOLEAN,                        -- NULL=발행 중, true=브로커 발행 성공, false=실패. 쿨다운은 false 아닌 행
    ack_result    TEXT,                           -- 카메라 ack result (ok | rejected_* ...)
    acked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS camera_ops_reboots_camera_requested_idx
    ON public.camera_ops_reboots (camera_id, requested_at DESC);

ALTER TABLE public.camera_ops_reboots ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.camera_ops_reboots FROM anon, authenticated;

COMMENT ON TABLE public.camera_ops_reboots IS
    '운영 재부팅(서버 시크릿 경로) 기록. 앱에 노출하지 않음. backend/routers/ops.py';
```

- [ ] **Step 2: 실패 테스트** `test_gte_filters_iso_strings` — 행 4개(`t` = 10:00, 11:00, 12:00 ISO `+00:00`, None)에 `.gte("t", 11:00)` → 2행.
- [ ] **Step 3: 실패 확인** `uv run pytest tests/test_fake_sb_ext.py -q` → FAIL.
- [ ] **Step 4: `_Query.gte` 구현**
- [ ] **Step 5: 통과 확인 후 커밋** `feat(ops): camera_ops_reboots 마이그레이션 + FakeSB gte`

### Task 2: `POST /ops/cameras/{camera_uuid}/reboot`

**Files:**
- Create: `backend/routers/ops.py`
- Modify: `backend/main.py` (include, `openapi_tags`에 `ops`, lifespan 시작 시 `OPS_REBOOT_SECRET` 미설정이면 `logger.warning` 1회)
- Test: `tests/test_ops_reboot_api.py`

**Context:**
- Depends on: Task 1
- Consumes: `backend.db_helpers.first_row`, `parse_uuid_or_404`; `backend.mqtt.camera_commands.reboot_command`; `backend.webrtc_signaling.MqttWebRTCSignaling`; `backend.ota_service.active_job_for`; `backend.supabase_client.get_supabase_client`; `backend.routers.cameras.RebootOut`(응답 모델 재사용)
- Produces: `router = APIRouter(prefix="/ops", tags=["ops"])`, `OPS_REBOOT_COOLDOWN_SEC = 7200`, `require_ops_secret(x_ops_secret: str | None = Header(None, alias="X-Ops-Secret")) -> None`
- Must know:
  - 핸들러는 `def`.
  - 409/429/503 본문은 평탄 형태라 `JSONResponse`로 반환: 409 `{"detail":"skipped","reason":<not_beta|offline|live_active|ota_active>}`, 429 `{"detail":"cooldown","retry_after":n}` + 헤더 `Retry-After: n`, 503 `{"detail":"ops log unavailable"}`. 401 `{"detail":"invalid ops secret"}`, 404 `{"detail":"camera not found"}`(없음·해제·잘못된 uuid).
  - 카메라 조회 컬럼: `id, camera_id, labeling_group, unlinked_at, is_online, live_session_id, stream_until`.
  - `now = datetime.now(timezone.utc)` 하나를 판정·기록에 같이 쓴다.
  - 쿨다운: `camera_ops_reboots` where `camera_id`, `requested_at` ≥ now-7200s, `order requested_at desc`, `limit 5` → 파이썬에서 `published is not False`인 첫 행. `retry_after = max(1, ceil(7200 - (now - requested_at)))`.
  - 카메라별 락: `live_session._lock_for`와 같은 패턴을 `ops.py` 안에 둔다. 쿨다운 조회 → INSERT → 발행 → UPDATE를 감싼다(publish 최대 5초, 단일 프로세스라 감수).
  - INSERT `{camera_id, msg_id: cmd["msg_id"], reason, requested_by, requested_at: now.isoformat(), published: None}`. 예외 → `logger.error` + 503, 발행 안 함.
  - 발행 예외 → `published=False` UPDATE, 200 `{"published": False, "msg_id": None}`. 성공 → `published=True` UPDATE, 200 `{"published": True, "msg_id": cmd["msg_id"]}`. UPDATE 예외는 `logger.error`, 응답은 발행 결과대로.
  - 로그: `logger.info("ops reboot 발행 camera=%s msg_id=%s reason=%s by=%s", ...)`. 헤더 값 로그 금지.
  - 바디 `OpsRebootIn(reason: str = Field(min_length=1, max_length=64), requested_by: str = Field(min_length=1, max_length=64))`. 누락은 FastAPI 기본 422.
- Acceptance: `uv run pytest -q` 전체 통과(기존 `/cameras/{id}/reboot` 테스트 포함).

- [ ] **Step 1: 실패 테스트** — `tests/test_recordings_api.py`의 FakeSB·TestClient 픽스처 패턴 + `tests/test_cameras_api.py:304 _install_fake_signaling` 패턴(대상 모듈 `backend.routers.ops`). `monkeypatch.setenv("OPS_REBOOT_SECRET", "s3cret")`. 기본 카메라 `labeling_group="beta"`, `unlinked_at=None`, `is_online=True`, `live_session_id=None`, `stream_until=None`, `camera_id="p4cam-beta"`. msg_id·uuid는 모두 UUID 문자열.
  - `test_ops_reboot_publishes_and_records` → 200 `{"published": True, "msg_id": <uuid str>}`, publish 1회 `("p4cam-beta", cmd)` `cmd["action"]=="reboot"`, 기록 1행 `published is True`, `msg_id`==응답, `reason=="night_rec_stall"`, `requested_by=="beta-camera-watch"`, `requested_at` 채워짐.
  - `test_ops_reboot_401_missing_header`, `test_ops_reboot_401_wrong_secret` → 401, publish 0, 기록 0.
  - `test_ops_reboot_401_when_server_secret_unset` → env 삭제 + 헤더 `""` → 401.
  - `test_ops_reboot_404_unknown_and_unlinked` → 없는 uuid·해제 카메라·`"not-a-uuid"` 모두 404.
  - `test_ops_reboot_409_not_beta` (`labeling_group=None`), `test_ops_reboot_409_offline` (`is_online=False`), `test_ops_reboot_409_live_session` (`live_session_id="s-1"`) → 각 reason, publish 0, 기록 0.
  - `test_ops_reboot_409_ota_active` → `ota_jobs`에 `target_uuid`=카메라, `status="downloading"` 행 → `reason=="ota_active"`.
  - `test_ops_reboot_live_by_stream_until_only` → `stream_until`=now+60s → `live_active`; now-60s → 200.
  - `test_ops_reboot_429_cooldown` → 200 후 즉시 재호출 429, `7190 <= retry_after <= 7200`, `Retry-After == str(retry_after)`, publish 총 1회.
  - `test_ops_reboot_cooldown_expired` → 기록 `published=True, requested_at=now-7201s` 선삽입 → 200.
  - `test_ops_reboot_pending_row_blocks` → 기록 `published=None, requested_at=now-10s` 선삽입 → 429.
  - `test_ops_reboot_failed_publish_does_not_start_cooldown` → signaling 예외 → 200 `{"published": False, "msg_id": None}`, 기록 `published is False`; 정상 signaling으로 재호출 → 200 published.
  - `test_ops_reboot_insert_failure_fails_closed` → `camera_ops_reboots` insert가 예외를 던지게 패치 → 503 `{"detail":"ops log unavailable"}`, publish 0.
  - `test_ops_reboot_422_missing_body_fields` → `reason` 없음 → 422.
- [ ] **Step 2: 실패 확인** `uv run pytest tests/test_ops_reboot_api.py -q` → FAIL.
- [ ] **Step 3: 구현** (`ops.py` + `main.py`).
- [ ] **Step 4: 통과 확인** `uv run pytest -q`.
- [ ] **Step 5: 커밋** `feat(ops): 운영 카메라 재부팅 API — 서버 시크릿·베타 한정·라이브/OTA 보호·2시간 쿨다운·기록 먼저`

### Task 3: 카메라 ack → `camera_ops_reboots.ack_result`

**Files:**
- Modify: `backend/mqtt/handlers.py` (`_ack_camera`에서 호출)
- Test: `tests/test_mqtt_handlers.py`

**Context:**
- Depends on: Task 1
- Consumes: `backend.db_helpers.parse_uuid`
- Produces: `_record_ops_reboot_ack(sb: Any, payload: dict[str, Any]) -> None`
- Must know: 건너뛰는 ack — `action`이 `webrtc_`로 시작, `ota` 블록 있음, `result` 없음, `msg_id`가 UUID로 안 파싱됨(실 DB에선 22P02). 그 외에는 `update({"ack_result": result, "acked_at": now}).eq("msg_id", <정규화 uuid>).is_("ack_result", "null")` 1회. 대상 행 없으면 0행(set_rotation 등)으로 끝. 예외는 `logger.exception`으로 삼키고 `last_seen` 갱신은 계속. 이 파일의 기존 테스트는 MagicMock `fake_sb` 기반 — 새 테스트는 `FakeSB`를 `handlers.get_supabase_client`에 monkeypatch해서 쓴다.
- Acceptance: `uv run pytest -q` 통과, 특히 기존 `test_handle_ack_camera_updates_last_seen_only`·`…logs_msg_id_and_result` 무수정 통과.

- [ ] **Step 1: 실패 테스트** (msg_id는 UUID 문자열 `M1`)
  - `test_camera_ack_records_ops_reboot_result` → 행 `msg_id=M1, ack_result=None`; `handle_ack(CAMERA_TEXT, {"msg_id": M1, "result": "ok"})` → `ack_result=="ok"`, `acked_at` 채워짐.
  - `test_camera_ack_rejected_reboot_recorded` → `"rejected_unknown_action"` 그대로 저장.
  - `test_camera_ack_skips_non_ops_acks` → `{"action":"webrtc_answer","msg_id":M1,"result":"ok"}`, `{"msg_id":M1,"result":"ok","ota":{...}}`, `{"msg_id":"m-1","result":"ok"}`, `{"msg_id":M1}` 각각 후 행 `ack_result is None`.
- [ ] **Step 2: 실패 확인 → Step 3: 구현 → Step 4: `uv run pytest -q` 통과**
- [ ] **Step 5: 커밋** `feat(ops): 카메라 ack 를 운영 재부팅 기록에 채움`

### Task 4: 문서 5종 + petcam 회신서

**Files:**
- Modify: `docs/API.md` (§4.12 운영 재부팅 + §8 변경 이력 0.11.0), `docs/DATABASE.md` (`camera_ops_reboots`), `docs/ENV.md` (`OPS_REBOOT_SECRET`, 🔴 Critical 등급, 생성 `python -c "import secrets; print(secrets.token_urlsafe(32))"`), `.env.example` (`OPS_REBOOT_SECRET=` 빈 값)
- Create: `docs/BACKEND_HANDOFF_REPLY_OPS_CAMERA_REBOOT_2026-10-20.md`

**Context:**
- Depends on: Task 2, 3
- Must know: 회신서 내용 —
  - ① 경로·헤더는 요청서 그대로.
  - ② 응답 표: 요청서 2-1 + **추가 409 `ota_active`·503 `ops log unavailable`**(사유 포함) + 422.
  - ③ `result`를 `published`·`ack_result`·`acked_at`으로 나눔.
  - ④ 2-3 결론: 앱은 누른 재시작에만 반응(근거 파일) → 컬럼 불필요, 앱 담당 최종 확인 요청.
  - ⑤ 쿨다운은 발행 실패를 세지 않음.
  - ⑥ 건너뜀은 기록 안 함.
  - ⑦ `labeling_group` 가드의 한계(스크립트가 직접 쓰는 값).
  - ⑧ 배포 순서: 마이그레이션 → MIGRATIONS_APPLIED 기록 → `.env` 시크릿 → 배포 + **API·terra-bridge 둘 다 재시작** → beta 카메라 수 확인 → 요청서 4절 검증.
  - ⑨ 시크릿 전달은 owner가 안전 채널로.
  - ⑩ 계획서 경로·PR 링크(요청서 frontmatter `commit_sha`·`implementation_host`는 owner가 채움).
- Acceptance: `grep -n "OPS_REBOOT_SECRET\|camera_ops_reboots\|/ops/cameras" docs/*.md .env.example` 에서 5개 파일 모두 같은 이름으로 나온다.

- [ ] **Step 1: 문서 갱신 → Step 2: grep 확인 → Step 3: 커밋** `docs(ops): 운영 재부팅 API·테이블·env + petcam 회신서`

### Task 5: PR

- [ ] `git push -u origin feat/ops-camera-reboot` → `gh pr create`. 본문: 요약, **마이그레이션 선적용 필수**, `.env OPS_REBOOT_SECRET` 필요, API·terra-bridge 재시작, 검증 체크리스트(요청서 4절). 코드 리뷰는 사용자가 `/code-review`로. 머지·배포는 관훈님.
