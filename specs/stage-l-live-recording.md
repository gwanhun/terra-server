# Stage L — 라이브 직접 녹화 업로드 Implementation Plan

> **구현 방식 (CAOF):** 이 계획을 task 단위로 구현한다. Critical 트랙 — Implementer 에이전트 분리(GATE 4). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 앱이 폰에서 만든 라이브 직접 녹화(≤60초 mp4 + jpg)를 R2에 올리고, 목록·재생·삭제할 수 있게 서버 API·테이블·정리 배치를 만든다.

**Architecture:** 새 테이블 `live_recordings`(RLS SELECT 본인만) + 새 라우터 `backend/routers/recordings.py`(등록·완료·URL·삭제, JWT). 업로드는 기존 클립처럼 presigned PUT로 앱이 R2에 직접 올린다. 24시간 넘은 `pending`은 API 프로세스의 백그라운드 스레드(`LiveSessionReaper` 패턴)가 지운다. 목록 API는 없다 — 앱이 Supabase에서 직접 SELECT.

**Tech Stack:** FastAPI, supabase-py(service_role), boto3(R2), pytest + `tests/fake_sb.py`

**Spec:** `docs/BACKEND_HANDOFF_REPLY_LIVE_RECORDING_2026-10-20.md` (계약 SOT). 원 요청: tera-ai-flutter `docs/handoffs/2026-10-20-live-recording-server-requests.md`

## Global Constraints

- 오류: 한도 초과 409 `{"detail":{"code":"recording_quota_exceeded","used":N,"limit":50}}` / 업로드 확인 실패 409 `{"detail":{"code":"upload_missing"}}` (+ 크기 불일치면 `"reason":"size_mismatch"`). 그 외 400·403·404는 문자열 `detail`, R2 실패는 502.
- 검사값: `duration_ms` 3,000~60,000 / `size_bytes` 1~30MB(`30 * 1024 * 1024`) / 썸네일 ≤ 5MB(`5 * 1024 * 1024`) / 한도 50개(`pending`+`ready`).
- presigned PUT 유효 **1,800초**(30분). GET은 기존 `DEFAULT_GET_URL_TTL`(3,600초), 응답 필드는 `expires_in`(초).
- R2 키: `terra-clips/recordings/{user_id}/{camera_id}/{recording_id}.mp4` / `.jpg`. `camera_id` = `cameras.id`(uuid). `recording_id`는 서버가 INSERT 전에 uuid4로 만든다(키와 행 id 일치 — clips와 같은 이유).
- 카메라 소유 확인은 `cameras.owner_id = user_id`만. **`unlinked_at` 은 보지 않는다**(해제 뒤 업로드 허용). 남의 것·없는 것 → 403 `"camera not found or not owned"`.
- 사용자 요청 경로의 `live_recordings` 쿼리는 전부 `.eq("user_id", user_id)` 명시. 예외: 카메라 소유 확인은 `cameras` 를 `.eq("owner_id", user_id)` 로, 정리 스레드는 전역(사용자 무관) 쿼리.
- 행 단건 조회는 `.single()` 금지 → `.limit(1)` 리스트 조회(실제 PostgREST 는 0행이면 406 → 500). 경로의 `recording_id` 는 `uuid.UUID()` 파싱 실패 시 404.
- `/recordings` 경로의 요청 검증 오류(FastAPI 422)는 400 + 문자열 `detail` 로 바꾼다(앱은 400만 비재시도).
- 한도 검사는 count 후 INSERT 라 서로 다른 `client_id` 동시 요청이면 1~2개 넘칠 수 있다 — 허용(검수 결정).
- 블로킹 I/O(supabase·boto3)라 핸들러는 전부 `def`(async 아님) — 기존 clips와 같다.
- 건드리지 않는 것: `motion_clips`·`backend/routers/clips.py`·기존 `/clips` 계약, 회원 탈퇴(보류), 보관 기간 정책(무기한), R2 lifecycle 설정 자체(대시보드에서 사람이 확인).

**실행 순서:** Task 0 → 1 → 2 → 6 → 3 → 4 → 5 → 7 (Task 3 이 Task 6 의 `purge_one` 을 쓴다)

## Review Focus

1. **같은 `client_id` 동시 두 번 호출** → 둘 다 같은 `recording_id`를 받아야 한다(UNIQUE 충돌 23505를 잡아 기존 행 재조회). → Task 3 `test_create_concurrent_duplicate_returns_existing`
2. **한도 49개 상태에서 50번째의 재호출** → 409가 아니라 기존 행 반환. → Task 3 `test_retry_same_client_id_at_quota_skips_quota`
3. **썸네일이 없거나 5MB 초과** → 영상은 살리고 썸네일만 버린다(`ready` + `thumbnail_key=null`). 409를 주면 앱이 영상 30MB까지 통째로 다시 올리고, 초과 파일은 같은 걸 또 올려 무한 반복된다. → Task 4 `test_complete_missing_thumbnail_dropped`, `test_complete_oversized_thumbnail_dropped`
4. **정리 스레드와 complete 경합** → 행을 먼저 조건부(pending) 삭제하고 지워진 행의 객체만 지운다. complete 가 먼저 ready 로 바꿨으면 그 행은 건드리지 않는다. → Task 6 `test_purge_skips_row_completed_meanwhile`
5. **해제된 카메라로 등록** → 201. → Task 3 `test_create_allows_unlinked_own_camera`

---

## 사전 조건 (사람)

- [ ] Cloudflare 대시보드 → R2 → 버킷 `petcam-clips` → Settings → Object lifecycle rules 에서 규칙 prefix 확인. `terra-clips/recordings/` 가 삭제 대상이면 배포 전에 규칙을 `terra-clips/clips/` 로 좁힌다. (2026-10-20: 서버 R2 키에 `GetBucketLifecycleConfiguration` 권한이 없어 스크립트 확인 실패 — AccessDenied) **버킷을 petcam-lab과 같이 쓰므로 규칙 변경은 petcam 쪽과 맞춘다.**

- [ ] **Task 0 승인 후** 회신서 변경분을 앱에 알리고 확인받는다(썸네일 정책은 계약 변경).

---

### Task 0: 회신서(SOT) 먼저 고치기 — 구현 전

**Files:** Modify `docs/BACKEND_HANDOFF_REPLY_LIVE_RECORDING_2026-10-20.md`

- 썸네일: "≤ 1MB, 위반 400" → "**complete 때 썸네일이 없거나 5MB를 넘으면 썸네일만 버리고 `ready`**(영상 재업로드 없음). 목록 `has_thumbnail=false`".
- `POST /recordings`: `ready` 재호출이면 `expires_at` 도 `null`. 재호출 시 바디는 무시하고 처음 등록한 값 기준. 24시간 넘은 `pending` 재호출은 새 녹화로 등록(201, 새 `recording_id`).
- 요청 형식 오류(필드 누락·타입)는 400.
- 한도 50은 동시 등록 시 1~2개 넘을 수 있음(설명 한 줄).
- Acceptance: 회신서와 이 계획의 숫자·동작이 일치. 커밋 `docs(recordings): 회신서 — 썸네일 버림 정책·재호출 규칙 명시`

### Task 1: 마이그레이션 + FakeSB 확장

**Files:**
- Create: `migrations/2026-10-20_live_recordings.sql`
- Modify: `tests/fake_sb.py` (`delete()`, `lt()` 추가)
- Test: `tests/test_fake_sb_ext.py`

**Context:**
- Depends on: 없음
- Produces: 테이블 `public.live_recordings`(아래 DDL), `FakeSB` 의 `.delete()`·`.lt(col, val)`
- Must know: `lt` 는 ISO 문자열 비교. 테스트 시각은 반드시 aware datetime 의 `.isoformat()`(`+00:00`)으로 — `Z` 를 섞으면 비교가 깨진다. `delete` 는 매칭 행을 지우고 지운 행 목록을 돌려준다(supabase-py 와 같음).
- Acceptance: `uv run pytest tests/test_fake_sb_ext.py -q` PASS

마이그레이션 전문:

```sql
-- 2026-10-20: 라이브 직접 녹화 (앱 요청 2026-10-20, 회신 docs/BACKEND_HANDOFF_REPLY_LIVE_RECORDING_2026-10-20.md)
create table if not exists public.live_recordings (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  camera_id     uuid references public.cameras(id) on delete set null,
  client_id     uuid not null,
  status        text not null default 'pending' check (status in ('pending', 'ready')),
  started_at    timestamptz not null,
  duration_ms   int not null,
  size_bytes    bigint not null,
  width         int,
  height        int,
  r2_key        text not null,
  thumbnail_key text,
  has_thumbnail boolean generated always as (thumbnail_key is not null) stored,
  created_at    timestamptz not null default now(),
  unique (user_id, client_id)
);

alter table public.live_recordings enable row level security;

drop policy if exists "live_recordings select own" on public.live_recordings;
create policy "live_recordings select own" on public.live_recordings
  for select to authenticated using (user_id = auth.uid());
-- INSERT/UPDATE/DELETE 정책 없음 = 서버(service_role)만.
grant select on public.live_recordings to authenticated;

create index if not exists live_recordings_user_started_idx
  on public.live_recordings (user_id, started_at desc);
create index if not exists live_recordings_status_created_idx
  on public.live_recordings (status, created_at);
```

- [ ] Step 1: 테스트 — `test_delete_removes_matching_rows`(eq 매칭 행만 사라지고 반환값에 들어감), `test_lt_filters_iso_strings`
- [ ] Step 2: 실패 확인 `uv run pytest tests/test_fake_sb_ext.py -q` → AttributeError
- [ ] Step 3: `_Query.delete(self) -> _Query`, `_Query.lt(self, col: str, val: Any) -> _Query` 추가, `_match`·`execute` 에 반영
- [ ] Step 4: PASS 확인 + 기존 `uv run pytest -q` 전체 PASS
- [ ] Step 5: 커밋 `feat(recordings): live_recordings 마이그레이션 + FakeSB delete/lt`

### Task 2: R2 크기 조회 헬퍼

**Files:**
- Modify: `backend/r2_client.py` (+ `__all__`)
- Test: `tests/test_r2_client.py`

**Context:**
- Depends on: 없음
- Produces: `head_object_size(key: str) -> int | None` — 있으면 `ContentLength`, 없으면 `None`. 404 외 오류는 그대로 raise(호출 측이 502).
- Must know: boto3 `head_object` 의 없는 키는 `ClientError` 이고 `response["Error"]["Code"]` 가 `"404"` / `"NoSuchKey"` / `"NotFound"` 중 하나.
- Acceptance: `uv run pytest tests/test_r2_client.py -q` PASS

- [ ] Step 1: 테스트 — `test_head_object_size_returns_length`(stub client `head_object` → `{"ContentLength": 123}` → 123), `test_head_object_size_missing_returns_none`(ClientError code "404" → None), `test_head_object_size_other_error_raises`(code "AccessDenied" → raise)
- [ ] Step 2: 실패 확인
- [ ] Step 3: 구현
- [ ] Step 4: PASS
- [ ] Step 5: 커밋 `feat(r2): head_object_size`

### Task 3: `POST /recordings` 등록 + 업로드 URL

**Files:**
- Create: `backend/routers/recordings.py`
- Modify: `backend/main.py` (`include_router(recordings.router)`, openapi tag)
- Create: `tests/test_recordings_api.py`

**Context:**
- Depends on: Task 1, 2 (fixture 가 `head_object_size` 를 patch), 6 (`purge_one`)
- Produces: `router = APIRouter(prefix="/recordings", tags=["recordings"])`, 상수 `RECORDING_PUT_URL_TTL = 1800`, `RECORDING_QUOTA = 50`, `MIN_DURATION_MS = 3000`, `MAX_DURATION_MS = 60000`, `MAX_VIDEO_BYTES = 30 * 1024 * 1024`, `MAX_THUMB_BYTES = 5 * 1024 * 1024`; 헬퍼 `_build_key(user_id: str, camera_id: str, recording_id: str, ext: str) -> str`, `_load_recording_for_owner(sb, recording_id: str, user_id: str) -> dict` (uuid 파싱 실패·없음·남의 것 → 404 `"recording not found"`, `.limit(1)` 조회)
- Must know:
  - 요청 모델 `RecordingCreate`: `camera_id: UUID, client_id: UUID, started_at: datetime, duration_ms: int, size_bytes: int, width: int|None, height: int|None, has_thumbnail: bool`. 범위 위반은 pydantic `Field(ge/le)` → FastAPI 422 가 아니라 **400** 이어야 한다(앱은 400만 비재시도) → 핸들러 안에서 직접 검사해 `HTTPException(400)`.
  - 응답 `RecordingUpload`: `recording_id, status, video_upload_url: str|None, thumb_upload_url: str|None, expires_at: datetime|None`. 새 행 201, 기존 행 200 (`Response.status_code` 로 분기).
  - 순서 고정: ① `(user_id, client_id)` 행 조회 → `ready` 면 URL 전부 null·`expires_at` null 로 200 / `pending` 이고 `created_at` 이 24시간 이내면 **저장된 `r2_key`·`thumbnail_key` 로** 새 PUT URL 발급해 200(새 바디는 무시) / `pending` 인데 24시간 넘었으면 Task 6 의 `purge_one(sb, row, delete_fn)` 로 지우고 ②로 진행 ② 카메라 소유 확인(403) ③ 한도: 그 user 행 수(`len(select("id")...data)`, 상태 무관) ≥ 50 → 409 ④ uuid4 → 키 → INSERT ⑤ INSERT 가 `APIError` 이고 `exc.code == "23505"` 면 ①로 재조회해 200(그 외 APIError 는 그대로).
  - INSERT 컬럼은 **화이트리스트**: `id, user_id, camera_id, client_id, status, started_at, duration_ms, size_bytes, width, height, r2_key, thumbnail_key`. `has_thumbnail` 은 생성 컬럼이라 넣으면 Postgres 가 거부한다(FakeSB 는 통과하므로 테스트로 막는다).
  - `main.py` 에 `RequestValidationError` 핸들러: `request.url.path.startswith("/recordings")` 면 400 `{"detail": "invalid request: <첫 오류 loc·msg>"}`, 아니면 FastAPI 기본(422) 동작 그대로.
  - PUT URL: 영상 `generate_presigned_put_url(key, "video/mp4", RECORDING_PUT_URL_TTL)`, 썸네일 `"image/jpeg"`. 발급 실패(`BotoCoreError`, `ClientError`) → 502.
  - `tests/conftest.py` 의 `app_client` 루프에 recordings 모듈 추가하지 말고, 이 테스트 파일 전용 fixture 에서 `FakeSB` + R2 stub(`generate_presigned_put_url`, `generate_presigned_get_url`, `delete_object`, `head_object_size` 를 dict 저장소로) 를 `backend.routers.recordings` 에 monkeypatch.
- Acceptance: `uv run pytest tests/test_recordings_api.py -q` PASS

- [ ] Step 1: 테스트 (카메라 행 `{"id": CAM, "owner_id": TEST_USER_ID, "unlinked_at": None}`)
  - `test_create_returns_201_with_put_urls` — 201, `status=="pending"`, video URL 에 `terra-clips/recordings/{TEST_USER_ID}/{CAM}/{recording_id}.mp4`, thumb URL `.jpg`, `expires_at` ≈ now+1800s
  - `test_create_without_thumbnail_thumb_url_null`
  - `test_create_other_users_camera_403`, `test_create_unknown_camera_403`
  - `test_create_allows_unlinked_own_camera` — `unlinked_at` 채운 본인 카메라 → 201
  - `test_create_duration_out_of_range_400` (2999, 60001), `test_create_size_too_large_400` (`30*1024*1024+1`), `test_create_size_zero_400`
  - `test_create_quota_exceeded_409` — 기존 50행(pending 섞음) → 409, `detail == {"code":"recording_quota_exceeded","used":50,"limit":50}`
  - `test_retry_same_client_id_pending_returns_200_new_urls` — 같은 `recording_id`, 행 수 그대로
  - `test_retry_same_client_id_at_quota_skips_quota` — 본인 행 50개 중 하나의 client_id 로 재호출 → 200
  - `test_retry_same_client_id_ready_returns_null_urls` — 200, `status=="ready"`, URL 둘 다 null, `expires_at` null
  - `test_create_concurrent_duplicate_returns_existing` — INSERT 가 `APIError({"message": "duplicate key", "code": "23505", "details": None, "hint": None})` 를 던지도록 하고 그 직전에 같은 client_id 행을 넣어 둠 → 200, 그 행의 id
  - `test_retry_same_client_id_ignores_new_body` — 재호출 바디의 `has_thumbnail=false`·다른 camera → 저장된 키로 URL, thumb URL 그대로
  - `test_retry_stale_pending_registers_new` — 25시간 전 pending 같은 client_id → 201, 새 `recording_id`, 옛 행·객체 삭제
  - `test_insert_payload_has_no_generated_column` — 저장된 행에 `has_thumbnail` 키가 없음
  - `test_create_put_urls_content_type_and_ttl` — stub 이 받은 인자: 영상 `("video/mp4", 1800)`, 썸네일 `("image/jpeg", 1800)`
  - `test_create_presign_failure_502`
  - `test_create_malformed_body_400` — `camera_id: "abc"` → 400, `detail` 문자열
  - 오류 테스트는 전부 `res.json()["detail"]` 정확값까지 비교(403 `"camera not found or not owned"` 등)
- [ ] Step 2: 실패 확인 (ImportError — 모듈 없음)
- [ ] Step 3: 라우터 + `create_recording(body, response, user_id=Depends(get_current_user_id)) -> RecordingUpload` 구현, `main.py` 등록
- [ ] Step 4: PASS + 전체 `uv run pytest -q` PASS
- [ ] Step 5: 커밋 `feat(recordings): POST /recordings 등록 + 업로드 URL`

### Task 4: `POST /recordings/{id}/complete`

**Files:** Modify `backend/routers/recordings.py`, `tests/test_recordings_api.py`

**Context:**
- Depends on: Task 2, 3
- Produces: `complete_recording(recording_id: str, user_id) -> {"status": "ready"}`
- Must know: 이미 `ready` → 바로 200. 영상 `head_object_size(r2_key)` None → 409 `{"code":"upload_missing"}`; `!= size_bytes` → 409 `{"code":"upload_missing","reason":"size_mismatch"}`. 썸네일(`thumbnail_key` 있을 때)이 None 이거나 `> MAX_THUMB_BYTES` → **버린다**: UPDATE 를 `{"status":"ready","thumbnail_key":None}` 한 번으로 하고, 그다음 썸네일 객체 `delete_object`(실패는 로그만). 상태 갱신은 `.eq("id").eq("user_id").eq("status","pending")`; **0행이면 재조회** → `ready` 면 200, 없으면 404. R2 오류 → 502.
- Acceptance: 아래 테스트 PASS

- [ ] Step 1: 테스트 — `test_complete_marks_ready`, `test_complete_missing_video_409`(detail 정확값), `test_complete_size_mismatch_409_reason`, `test_complete_missing_thumbnail_dropped`(200, `thumbnail_key is None`), `test_complete_oversized_thumbnail_dropped`(200, `thumbnail_key is None`, 썸네일 객체 삭제됨), `test_complete_already_ready_200`, `test_complete_other_users_404`, `test_complete_bad_uuid_404`, `test_complete_head_error_502`, `test_complete_row_vanished_during_update_404`
- [ ] Step 2: 실패 확인
- [ ] Step 3: 구현
- [ ] Step 4: PASS
- [ ] Step 5: 커밋 `feat(recordings): complete — R2 존재·크기 확인`

### Task 5: 재생·썸네일 URL + 삭제

**Files:** Modify `backend/routers/recordings.py`, `tests/test_recordings_api.py`

**Context:**
- Depends on: Task 3
- Produces: `GET /recordings/{id}/url`, `GET /recordings/{id}/thumbnail/url` → `{"url", "expires_in": 3600}` (clips 의 `ClipUrl` 모양 재사용 — import 해서 쓴다); `DELETE /recordings/{id}` → 204
- Must know: URL 은 `ready` 만, `pending` 은 404. 썸네일 키 없으면 404. 삭제는 `pending` 포함 본인 행이면 204: 영상·썸네일 `delete_object`(실패 시 `logger.error("recording r2 delete failed key=%s", key)` 후 계속) → 행 delete `.eq("id").eq("user_id")`. 남의 것·없음 404.
- Acceptance: 아래 테스트 PASS

- [ ] Step 1: 테스트 — `test_get_url_ready`(`expires_in == 3600`), `test_get_thumbnail_url_ready`, `test_get_url_presign_failure_502`, `test_get_url_pending_404`, `test_get_thumbnail_url_none_404`, `test_get_url_other_users_404`, `test_delete_ready_removes_row_and_objects`, `test_delete_pending_204`, `test_delete_other_users_404`, `test_delete_r2_failure_still_deletes_row`(delete stub 이 ClientError → 204, 행 없음, caplog 에 키)
- [ ] Step 2~4: 실패 확인 → 구현 → PASS
- [ ] Step 5: 커밋 `feat(recordings): 재생 URL + 삭제`

### Task 6: `pending` 24시간 정리 스레드

**Files:**
- Create: `backend/recording_cleanup.py`
- Modify: `backend/main.py` (`_lifespan` 에서 start/stop — `LiveSessionReaper` 옆)
- Test: `tests/test_recording_cleanup.py`

**Context:**
- Depends on: Task 1
- Produces: `PENDING_TTL = timedelta(hours=24)`, `CLEANUP_INTERVAL_SEC = 3600.0`, `purge_one(sb, row: dict, delete_fn: Callable[[str], None]) -> bool`(행을 실제로 지웠으면 True — Task 3 도 씀), `purge_stale_pending(sb, now: datetime, delete_fn: Callable[[str], None]) -> int`(지운 행 수), `class PendingRecordingReaper(sb_factory, delete_fn, interval_sec=CLEANUP_INTERVAL_SEC)` — `start()/stop()`, 스레드 이름 `"recording-pending-reaper"`. 구조는 `backend/live_session.py:193` `LiveSessionReaper` 그대로.
- Must know: 대상 = `status='pending'` and `created_at < now-24h`. `purge_one` 순서: **행 먼저** `.delete().eq("id").eq("status","pending")` → 지워진 행이 있을 때만 영상·썸네일 객체 삭제(실패는 `logger.error("recording r2 delete failed key=%s", key)` 후 계속). 그 사이 complete 된 행은 0행 삭제라 객체도 안 건드린다. Task 3 이 import 하므로 Task 3 보다 먼저 한다(실행 순서 참고). 한 번에 최대 200행(`limit`). 루프 예외는 `logger.exception` 후 계속.
- Acceptance: `uv run pytest tests/test_recording_cleanup.py -q` PASS

- [ ] Step 1: 테스트 — `test_purges_pending_older_than_24h`(25h 전 pending 지움, 23h 전 pending·25h 전 ready 남김, 반환 1, 삭제된 키 = mp4+jpg), `test_purge_continues_on_delete_error`, `test_purge_skips_row_completed_meanwhile`(대상 조회 뒤 행을 ready 로 바꿔 두면 → 행 남고 객체 삭제 호출 0)
- [ ] Step 2~4: 실패 확인 → 구현 → PASS, 전체 `uv run pytest -q` PASS
- [ ] Step 5: 커밋 `feat(recordings): pending 24h 정리 스레드`

### Task 7: 문서

**Files:** Modify `docs/API.md`(녹화 섹션 + 엔드포인트 표), `docs/DATABASE.md`(`live_recordings` 항목), 이 파일 상태 갱신

- Depends on: Task 3~6
- Acceptance: 문서의 경로·코드·상수가 구현과 일치(`grep -n "1800\|recording_quota_exceeded\|terra-clips/recordings" docs/API.md`)
- [ ] 커밋 `docs(recordings): API·DB 문서`

### 마무리

- [ ] `uv run pytest -q` 전체 PASS (exit 0)
- [ ] PR 생성 (base `main`). 본문에 배포 순서: ① lifecycle 사전 조건 ② 마이그레이션 SQL Editor 적용 → `MIGRATIONS_APPLIED.md` 기록 ③ **스모크**: 실제 Supabase·R2 로 POST→curl PUT(`Content-Type: video/mp4`)→complete→GET url→DELETE 한 바퀴(생성 컬럼 INSERT·`.limit(1)`·HEAD 확인 — FakeSB 로는 못 잡음) ④ 서버 배포 ⑤ 회신서 "배포 상태" 체크
- [ ] 코드 검수는 사용자가 `/code-review` 호출

## 검증 방법 (구현 전 확정)

| 무엇 | 방법 |
|---|---|
| API 동작·오류 계약 | pytest (위 테스트 이름) |
| 마이그레이션 문법 | 사용자가 Supabase SQL Editor 적용 시 확인 — **서버 로컬에서 검증 불가** |
| 실제 Supabase·R2 왕복 | PR 본문 배포 순서 ③ 스모크 — 마이그레이션 적용 후 가능 |
| lifecycle | 대시보드 — 사용자만 가능 |

## 가정

- 확인: 소프트 해제는 카메라 행을 지우지 않는다(`backend/unlink_service.py:9`). 기존 에러 객체 형식이 있다(`backend/routers/webrtc.py:305`). R2 버킷 `petcam-clips` 를 petcam-lab과 공유.
- 미확인: lifecycle 규칙 범위. 앱이 보내는 `camera_id` 가 `cameras.id`(uuid) 라는 것 — 요청서 예시가 `"uuid"` 라 그렇게 본다(텍스트 `p4cam-…` 이면 403). uvicorn 워커가 여러 개면 정리 스레드도 여러 개 — 정리는 멱등이라 무해(중복 삭제 시 0행).

## 실패 시나리오

- lifecycle 이 버킷 전체 → 녹화가 30일 뒤 사라짐 → 사전 조건으로 막음.
- presigned PUT `Content-Type` 불일치 → 앱 업로드 403(SignatureDoesNotMatch) → 회신서에 명시됨.
- 마이그레이션 미적용 상태로 배포 → `/recordings` 500. 앱은 배포 체크 후에만 붙으므로 영향 없음. 순서는 PR 본문에.

## 사용자가 정할 것

- **썸네일 정책 변경 (계약 변경):** complete 때 썸네일이 없거나 5MB 초과면 썸네일만 버리고 `ready`. 회신서는 "1MB 초과 400" 이었는데 등록 시점엔 썸네일 크기를 알 수 없어 그대로는 구현 불가. 승인하면 Task 0 으로 회신서를 고치고 앱에 알린다.

검수: `reports/stage-l-live-recording-review-final.md` (Codex + Claude, 2026-10-20) 반영 완료.
