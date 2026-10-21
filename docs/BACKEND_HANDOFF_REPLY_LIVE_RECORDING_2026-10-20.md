# 라이브 직접 녹화 — 회신 (2026-10-20)

> 요청: tera-ai-flutter `docs/handoffs/2026-10-20-live-recording-server-requests.md`
> 오류 형식 회신(앱 → 서버, 2026-10-20) 반영
> 앱 2차 회신(2026-10-20) 반영 — 확인 요청 A·B 수용, 추가 확인 4건 답변(맨 아래)
> **3차 갱신(2026-10-20, 구현 전 검수 반영)** — 썸네일 정책·재호출 규칙·형식 오류 400. 아래 "변경 사항 (3차)" 참고

## 결론

요청 1·2·4·5·6을 받습니다. **요청 3(목록)은 Supabase 직결 SELECT**로 합니다(REST 목록 엔드포인트 없음).
움직임 영상(`motion_clips`)과 섞지 않고 별도 테이블 `live_recordings`·별도 R2 경로를 씁니다.

상태: **배포 완료 (2026-10-20, terra-server #20).** 실기 스모크는 앱 업로드 연동 때 함께 확인합니다.

---

## API

### `POST /recordings` — 등록 + 업로드 URL 발급 (JWT)

요청 바디는 요청서 그대로입니다(`camera_id`, `client_id`, `started_at`, `duration_ms`, `size_bytes`, `width`, `height`, `has_thumbnail`).

```
→ 201 (새 등록) / 200 (같은 client_id 재호출)
{
  "recording_id": "uuid",
  "status": "pending" | "ready",
  "video_upload_url": "https://...presigned PUT" | null,
  "thumb_upload_url": "https://...presigned PUT" | null,
  "expires_at": "..." | null     // ready 재호출이면 null
}
```

서버 처리 순서:

1. **같은 `(user_id, client_id)` 행 조회 — 한도 검사보다 먼저** (회신 확인 1)
   - `pending` 행이 있으면 → 200, 같은 `recording_id` + **새 업로드 URL**. 재호출 바디는 무시하고 처음 등록한 값을 기준으로 합니다
   - `ready` 행이 있으면 → 200, `status: "ready"`, 업로드 URL·`expires_at` 은 `null` (아래 "앱 확인 요청 A")
   - 등록한 지 **23시간 30분** 넘은 `pending` 이면 → 옛 행과 R2 객체를 **그 자리에서 먼저 지우고** 새 녹화로 등록(201, 새 `recording_id`). 그래서 unique 충돌도, 한 녹화가 한도를 2개 차지하는 일도 없습니다. 30분짜리 업로드 URL 이 24시간 정리와 겹치지 않게 하려는 것입니다
2. 없으면 카메라 소유 확인 → 한도 검사 → 새 행 `pending` 등록 → 201

- **카메라 소유 확인은 `owner_id`만 봅니다. 등록 해제(unlink)한 카메라도 허용합니다.**
  녹화 뒤 카메라를 해제하고 다음 실행 때 업로드해도 거절되지 않게 하려는 것입니다. 남의 카메라는 403.
- 검사: `duration_ms` 3,000~120,000(2026-10-21 60,000에서 상향, 아래 절) / `size_bytes` ≤ 30MB. 위반·필드 누락·형식 오류는 400(문자열 `detail`).
- 썸네일 크기는 등록 때 알 수 없어서 `complete` 에서 봅니다(아래).
- 업로드 URL 유효 시간: **30분.** 만료되면 같은 `client_id`로 다시 부르면 됩니다.
- PUT 할 때 `Content-Type`을 영상은 `video/mp4`, 썸네일은 `image/jpeg`로 보내야 서명이 맞습니다.
- 24시간이 지나 정리된 녹화를 같은 `client_id`로 다시 부르면 **새 녹화로 등록**합니다(이때는 한도 검사 포함). (회신 확인 2)
- 재호출이면 바디 범위 검사도 하지 않습니다(바디 무시).

### `POST /recordings/{id}/complete` (JWT)

```
→ 200 { "status": "ready" }
→ 409 { "detail": { "code": "upload_missing" } }                            객체 없음
→ 409 { "detail": { "code": "upload_missing", "reason": "size_mismatch" } } 크기 불일치
```

- 서버가 R2의 영상이 있는지, 크기가 등록값과 맞는지 확인합니다. (회신 확인 3)
- **썸네일이 없거나 5MB를 넘으면 썸네일만 버리고 `ready`로 처리합니다.** 영상을 다시 올릴 필요가 없습니다. 이때 목록의 `has_thumbnail` 은 `false` 입니다.
- 이미 `ready`면 그대로 200.

### `GET /recordings/{id}/url` · `GET /recordings/{id}/thumbnail/url` (JWT)

`GET /clips/{id}/url`과 같습니다. `{ "url", "expires_in": 3600 }` (요청서의 `expires_at` 대신 기존 클립과 같은 초 단위 `expires_in`). 썸네일이 없거나 남의 녹화면 404.

### `DELETE /recordings/{id}` (JWT) → 204

행과 R2 영상·썸네일을 함께 지웁니다. 남의 녹화면 404.
**`pending`도 똑같이 204로 지웁니다**(올라가다 만 객체 포함, 객체가 아직 없어도 성공).
**업로드 중인 녹화는 업로드를 먼저 취소한 뒤 `DELETE` 해 주세요.** 그래도 남는 파일(취소 직전에 PUT 완료 등)은 서버가 6시간마다 `terra-clips/recordings/` 를 훑어 **1시간 넘었고 어떤 녹화 행도 가리키지 않는 파일**을 지웁니다.

### 목록 — Supabase 직결

```dart
supabase.from('live_recordings')
  .select('id, camera_id, started_at, duration_ms, size_bytes, width, height, has_thumbnail, created_at')
  .eq('status', 'ready')
  .order('started_at', ascending: false)
  .order('id', ascending: false)
  .limit(30);
// 다음 페이지: 마지막 항목의 (started_at, id) 이후로 keyset
// 카메라별: .eq('camera_id', ...)
```

- RLS `user_id = auth.uid()` 라 본인 것만 보입니다.
- 카메라를 해제해도 녹화는 목록에 남습니다. 운영 처리로 카메라 행이 완전히 지워지면 `camera_id`가 `null`이 됩니다 → "삭제된 카메라"로 표시해 주세요.

---

## 오류 형식

| 상황 | 응답 | 앱 처리 |
|---|---|---|
| 한도 초과 | 409 `{"detail":{"code":"recording_quota_exceeded","used":50,"limit":50}}` | 안내 문구 |
| 업로드 확인 실패 | 409 `{"detail":{"code":"upload_missing"[,"reason":"size_mismatch"]}}` | URL 다시 받아 처음부터 업로드 |
| 남의 카메라 | 403 문자열 `detail` | 재시도 안 함 |
| 없음·남의 녹화 | 404 문자열 `detail` | 재시도 안 함 |
| 크기·길이 초과 | 400 문자열 `detail` | 재시도 안 함 |
| R2 응답 실패 | 502 문자열 `detail` | **재시도** (앱 확인 요청 B) |

`detail`이 객체면 `code`로 나누는 방식은 라이브 시청 제한(`live_in_use`, `live_cooldown`)과 같습니다.

---

## 정책

| 항목 | 결정 |
|---|---|
| 한도 | 계정당 **50개.** `ready` + `pending`을 함께 셉니다(대기 행으로 한도를 피해 가지 못하게). 거의 동시에 여러 개를 등록하면 1~2개 넘칠 수 있습니다 |
| 보관 기간 | **무기한**(사용자가 지울 때까지). 일괄 정리 정책은 나중에 따로 정합니다 |
| `pending` 정리 | 서버가 **24시간** 넘은 `pending` 행과 R2 객체를 지웁니다. 아직 안 지워진 24시간 넘은 `pending` 은 한도에 세지 않습니다 |
| 고아 파일 정리 | 6시간마다 녹화 경로를 훑어 1시간 넘었고 행이 가리키지 않는 파일을 지웁니다(업로드 중 삭제·R2 삭제 실패 대비) |
| R2 경로 | `terra-clips/recordings/{user_id}/{camera_id}/{recording_id}.mp4` (+ `.jpg`). 앱은 경로를 만들거나 파싱하지 않습니다 |
| 회원 탈퇴 | 이번 범위 밖(보류). 탈퇴 함수를 만들 때 `terra-clips/recordings/{user_id}/` 경로 삭제를 함께 넣습니다 |
| 셀룰러 업로드·폰 파일 정리·공유·워터마크 | 앱 결정대로(서버 무관) |

## 테이블

요청서 DDL에 아래를 더합니다. `width`·`height`는 그대로 유지합니다.

```sql
-- 앱이 thumbnail_key 를 직접 읽지 않도록 (앱 2차 회신 1)
has_thumbnail boolean generated always as (thumbnail_key is not null) stored
-- status 값 제한
check (status in ('pending', 'ready'))
-- pending 정리 배치용
create index on public.live_recordings (status, created_at);
```

INSERT/UPDATE/DELETE는 서버(service_role)만 합니다.

---

## 앱 확인 요청

**A. `ready`인 녹화를 같은 `client_id`로 다시 부르는 경우**
업로드를 마쳤는데 `complete` 응답 직전에 앱이 꺼지면 생길 수 있습니다. 이미 확정된 파일을 덮어쓰지 않도록 업로드 URL 없이 `status: "ready"`만 돌려줍니다. **앱은 `status`가 `ready`면 업로드를 건너뛰고 완료로 처리**해 주세요.

**B. 재시도 범위**
재시도하지 않는 건 400·403·404뿐으로 해 주세요. **502·5xx·네트워크 끊김은 일시적인 오류라 다시 시도**해야 합니다.

---

## 앱 2차 회신 답변

- **A·B 수용 확인.** 401 토큰 갱신 후 1회 재시도, `recording_quota_exceeded`는 삭제 후 재개 — 서버 쪽 추가 작업 없습니다.
1. **`has_thumbnail`** — 생성 컬럼으로 추가합니다(위 "테이블"). `width`·`height` 유지.
2. **`pending` 삭제** — 204로 지웁니다(위 `DELETE`). 404는 이미 없는 것으로 보셔도 됩니다.
3. **회원 탈퇴** — 이번 범위에서는 보류합니다. 탈퇴 함수를 만들 때 녹화 R2 삭제를 함께 넣겠습니다.
   경로는 `recordings/{user_id}/`가 아니라 **`terra-clips/recordings/{user_id}/`** 입니다.
4. **R2 lifecycle** — 확인되면 아래 첫 항목에 체크해 알려 드립니다.

## 변경 사항 (3차, 2026-10-20)

구현 전 검수에서 나온 것들입니다. **앱 쪽 확인 부탁드립니다** — 특히 1번은 계약 변경입니다.

1. **썸네일:** "≤ 1MB, 위반 400" → **`complete` 때 썸네일이 없거나 5MB를 넘으면 썸네일만 버리고 `ready`.** 등록 시점엔 썸네일 크기를 알 수 없고, 409로 거절하면 앱이 영상까지 다시 올리게 돼서 바꿨습니다.
2. **`ready` 재호출:** `expires_at` 도 `null`.
3. **재호출 바디:** 무시하고 처음 등록한 값 기준(업로드 URL 경로가 바뀌지 않게).
4. **23시간 30분 넘은 `pending` 재호출:** 정리 전이어도 옛 행을 지우고 새로 등록(201, 새 `recording_id`). 앱은 새 `recording_id` 로 갈아 끼워 주세요.
6. **업로드 중 삭제:** 업로드를 먼저 취소한 뒤 `DELETE`(코드리뷰 반영).
5. **형식 오류(필드 누락·타입 오류):** FastAPI 기본 422 대신 **400.** 앱의 "400은 비재시도" 규칙에 맞추려는 것입니다.

## 앱 3차 확인 답변 (2026-10-20)

- **1·2·3·5번:** 확인 감사합니다. 서버 추가 작업 없습니다.
- **4번:** 이미 요청하신 대로 동작합니다. 23시간 30분 넘은 `pending` 재호출은 옛 행과 R2 객체를 **그 자리에서 먼저 지운 뒤** 새로 넣습니다 → unique 충돌 없음, 한도 2개 차지 없음. 한도에서 빼는 기준은 24시간 그대로 둡니다(23.5~24시간 사이의 *다른* 녹화는 아직 `complete` 로 정상 완료될 수 있는 살아 있는 녹화라서).
- **6번:** 두 번째 안(경로 스캔)으로 했습니다. 6시간마다 `terra-clips/recordings/` 를 훑어 1시간 넘었고 어떤 행의 `r2_key`·`thumbnail_key` 도 아닌 파일을 지웁니다. 업로드 중 삭제뿐 아니라 R2 삭제 실패, 버린 썸네일 삭제 실패로 남는 파일도 같이 치웁니다. 앱 동작은 말씀하신 그대로(업로드 취소 확인 → `DELETE`, 앱 종료 시 다음 실행에 `DELETE` 재시도)면 됩니다. `DELETE` 재시도가 404 면 이미 지워진 것입니다.

## 길이 한도 120초 변경 (2026-10-21)

앱 요청서 `docs/handoffs/2026-10-21-live-recording-max-120s-request.md`(tera-ai-flutter) 회신입니다.

- `POST /recordings` 의 `duration_ms` 허용 범위를 **3,000~120,000** 으로 바꿨습니다. 범위 밖 400 `detail` 은 `"duration_ms must be between 3000 and 120000"`.
- `size_bytes` ≤ 30MB 와 업로드 URL 유효 시간 30분은 그대로입니다.
- DB 에는 길이 제약(CHECK)이 없어 마이그레이션은 없습니다. 서버 배포만 하면 반영됩니다.
- 배포 상태(질문 1): 아래 체크 그대로 마이그레이션·`/recordings` 전 엔드포인트는 2026-10-20 운영 반영돼 있습니다.

## 배포 상태

- [x] 운영 R2 lifecycle 규칙 확인 — 녹화 경로는 자동 삭제 대상 아님 (2026-10-20 대시보드 확인: `petcam-clips` 규칙은 미완료 멀티파트 7일 중단 + `terra-derived/gme/v1/debug-14d/` 14일 삭제 두 개뿐)
- [ ] 앱 3차 변경 사항 확인
- [x] 운영 DB 마이그레이션 `live_recordings` (2026-10-20)
- [x] 서버 배포 (API + `pending`·고아 객체 정리) (2026-10-20)
- [ ] 서버 배포 — 길이 한도 120초 (2026-10-21 변경)
