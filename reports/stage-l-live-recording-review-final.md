# 교차 검수 결과 (Codex + Claude) — specs/stage-l-live-recording.md

## 요약
- Codex: 12건, Claude: 17건 / 공통 지적: 6건

## 🔴 2모델 공통 — 수정
1. 썸네일 1MB 초과 처리(버리고 ready)는 회신서(SOT)와 다른 새 정책 → 구현 전에 회신서를 고치고 앱 확인을 받는다 (Task 0 신설)
2. 정리 스레드 race — R2 객체를 지운 뒤 행을 지우는 순서면, 그 사이 complete 된 행의 객체가 날아간다 → **행 먼저 조건부 삭제(pending), 실제로 지워진 행의 객체만 삭제**
3. complete 의 조건부 UPDATE 가 0행일 때 정의 없음 → 재조회해서 ready 면 200, 없으면 404
4. 같은 client_id 재호출 때 요청 바디가 기존 행과 다를 때 → **저장된 키 기준, 새 바디는 무시**
5. 한도 count→INSERT race(다른 client_id 동시 요청) → 채택 안 함. 한 사용자가 동시에 두 개를 등록하는 경우는 드물고 초과폭도 1~2개라 **허용하고 문서화**
6. 오류 계약 테스트 부족(정확한 detail, 썸네일 URL 성공, 502, Content-Type·TTL) → 테스트 추가

## 🟡 단독 지적 — 채택/기각
| 출처 | 지적 | 판단 |
|---|---|---|
| Claude | `.single()` 0행 → PostgREST 406 → 500(앱이 재시도 루프). FakeSB가 가림 | **채택** — `.limit(1)` 리스트 조회 + `uuid.UUID()` 파싱 실패 404 |
| Claude | 생성 컬럼 `has_thumbnail` 에 INSERT 값을 넣으면 Postgres 거부, FakeSB 통과 | **채택** — INSERT 컬럼 화이트리스트 명시 + 테스트 |
| Claude | `APIError("23505...")` 생성 자체가 AttributeError (직접 확인함) | **채택** — dict 생성 + `exc.code == "23505"` |
| Claude | 타입 오류는 422 → 앱이 무한 재시도 | **채택** — `/recordings` 경로만 422→400 변환 핸들러 |
| Claude | 24시간 넘은 pending 재호출이 새 URL을 받는 도중 정리 스레드가 지움 | **채택** — 재호출 시 24시간 넘은 pending 은 지우고 새로 등록(회신서 확인 2와 일치) |
| Claude | 썸네일 초과 처리에서 객체 삭제 후 UPDATE 실패 시 불일치 | **채택** — UPDATE(ready+thumbnail_key null) 먼저, 객체 삭제는 나중(실패는 로그) |
| Claude | 썸네일이 *없을* 때도 409 면 영상 30MB 재업로드 | **채택(사용자 확인 필요)** — 없어도 버리고 ready |
| Claude | Task 3 fixture 가 Task 2 함수를 patch → Task 2 의존 | **채택** |
| Claude | Task 3 실패 기대값은 404 가 아니라 ImportError | **채택** |
| Claude | 재시도 URL은 저장 키로 | **채택** (공통 4와 합침) |
| Claude | presigned PUT 에 ContentLength 서명 | **기각** — R2 강제 여부 미확인, 24h 정리로 상한 있음. BACKLOG 후보 |
| Claude | ready 재호출 시 `expires_at` null 을 문서에 | **채택** (Task 0) |
| Claude | FakeSB `lt` ISO 비교 — Z vs +00:00 | **채택** — 테스트 시각은 aware datetime `.isoformat()` |
| Claude | `(user_id, started_at)` 인덱스 과잉 | **기각** — 무해, 앱 목록 쿼리와 맞음 |
| Codex | 앱 직결 SELECT 용 `GRANT SELECT ... TO authenticated` 명시 없음 | **채택** — 마이그레이션에 명시 |
| Codex | "모든 쿼리 `.eq(user_id)`" 와 카메라 확인·정리 쿼리 충돌 | **채택** — 범위 명확화(사용자 요청 경로만) |
| Codex | 정리 중 R2 삭제 실패 → 무기한 orphan | **부분 채택** — 키를 error 로그로 남김. 재시도 큐는 보류(일괄 정리 로직 때 함께) |
| Codex | lifespan 통합 테스트 없음 | **기각** — 기존 `LiveSessionReaper` 도 없음. 수동 start/stop 은 단위 테스트로 충분 |

## 💡 직관 질문 답변
- **없어도 되는 것:** 정리 스레드를 POST 시점 정리로 대체 가능(Claude) → 기각. 안 돌아오는 사용자의 대기 행이 남는다. 스레드는 작다.
- **최대 리스크:** R2 lifecycle 이 버킷 전체면 녹화가 30일 뒤 조용히 사라진다. 테스트로 못 잡는다 → 사전 조건(대시보드 확인). 버킷을 petcam 과 공유해서 규칙 변경은 petcam 쪽과 맞춰야 한다.
- **먼저 해볼 것:** 실제 Supabase·R2 왕복(생성 컬럼 INSERT, `.limit(1)`, presigned PUT→HEAD). FakeSB 로는 못 잡는 #1~#3 류. 마이그레이션 적용 후 배포 전 스모크로 넣는다.

## 덤 — 범위 밖 발견
- `backend/routers/clips.py:270` `_load_clip_for_owner` 도 `.single()` 이라 없는 클립 id 에 404 대신 500 을 낼 가능성이 높다. 이번 PR 에서는 안 고친다(건드리지 않는 것).
