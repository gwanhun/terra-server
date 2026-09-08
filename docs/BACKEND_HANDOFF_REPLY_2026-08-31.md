# 백엔드 회신 — 커뮤니티 탭 크레캠 클립 공유 피드 (2026-08-31)

> **회신 대상**: 앱(Flutter) `backend-handoff-2026-08-29-community-clip-feed.md`
> **작성**: terra-server 백엔드 담당
> **성격**: 코드/스키마 실측 대조 결과 회신. terra-server 변경 없음이 맞고, §5 확인 요청 4건에 답변.
> **결론 먼저**: **충돌 없음 — 마이그레이션 진행해도 됨.** 단 §5-③(만료 클립)의 앱측 처리와, 하단 "부탁 사항" 1건만 반영해줘요.

---

## 0. 요약

| # | 확인 요청 | 판정 |
|---|---|---|
| §5-① | `community_*` 6종 + `public_profiles` 뷰 + `community-media` 버킷 이름 충돌 | ✅ **충돌 없음** — 진행 OK |
| §5-② | presigned GET 다운로드 → 복사 방식의 R2 egress 비용 | ✅ **문제 없음** — R2 는 egress 무료 |
| §5-③ | `motion_clips` 보존기한/자동 삭제 정책 | ⚠️ **정책상 30일, 실측 미확정** — 앱은 만료 클립 실패 처리 필수 (아래 상세) |
| §5-④ | 아바타 버킷이 타 유저에게 읽히는지 | ➡️ **앱 팀 소유 영역** — terra-server 에 정보 없음, 이니셜 폴백 시작 권장 |

terra-server 코드·API·스키마 변경 없음 확인. 클립 URL 계약(`GET /clips/{id}/url`, `GET /clips/{id}/thumbnail/url`, presigned GET TTL 1h, 본인 소유만)도 문서에 적힌 그대로 맞습니다.

---

## 1. §5-① 이름 충돌 / 운영 우려 → 없음

terra-server 가 소유한 public 스키마 객체는 마이그레이션 기준 아래가 전부:

```
enclosures, cameras, devices, device_settings, commands,
telemetry, telemetry_1m, telemetry_30m, alerts, schedules,
motion_clips, clip_favorites
(+ petcam-lab 소유 behavior_logs / behavior_labels 를 읽기·라벨 UPSERT 로 사용)
```

- `community_*`, `user_profiles`, `public_profiles`, `pets` 는 terra-server 코드·마이그레이션·문서 어디에도 없음 → 이름 충돌 없음.
- terra-server 는 **Supabase Storage 를 아예 안 씀** (미디어는 전부 Cloudflare R2). `community-media` 버킷·storage.objects 정책과 겹칠 게 없음.
- 마이그레이션은 SQL 수동 붙여넣기 방식이고 선언적 스키마 도구(supabase db push 등)를 안 쓰므로, "전체 스키마 관리 도구가 미등록 테이블을 밀어버리는" 시나리오 자체가 없음.
- `user_profiles.is_admin` 추가도 terra-server 는 해당 테이블을 읽지 않으므로 무관.

## 2. §5-② R2 egress 비용 → 문제 없음

- Cloudflare R2 는 **egress(다운로드 대역폭) 과금이 없음**. 글 1건당 presigned GET 2회(영상 수 MB + 썸네일)는 Class B 연산 요금인데 무시할 수준.
- 호출 주체가 클립 소유자 본인 JWT 라 기존 앱 내 재생과 동일 인증 경로·동일 규모 — 서버 관점 추가 부하 없음.
- 참고: 복사본 저장 비용은 R2 가 아니라 **Supabase Storage 용량**으로 쌓임 (건당 수 MB). 플랜 한도는 앱 팀에서 관리.

## 3. §5-③ motion_clips 보존정책 → 유일하게 주의할 부분

**정책 문서상** (docs/DATABASE.md):

- R2 영상 파일: **30일 lifecycle rule 로 자동 삭제**
- `motion_clips` 메타(DB row): 영구 보관

**실측/구현 현황**:

- R2 lifecycle rule 이 실제로 걸려 있는지는 **미확정** — 확인용 스크립트(`scripts/check_r2_lifecycle.py`)가 있고, 프로덕션에서 실행해 확정 후 결과를 별도 공유하겠음.
- R2 객체가 만료 삭제돼도 **DB row 동기 삭제는 미구현** (Stage F2 계획만 존재). 즉 `motion_clips` 행과 `clip_favorites` 행은 남는데 영상 실체만 사라질 수 있음.
- 즐겨찾기 클립을 만료에서 제외하는 것도 현재 불가 — lifecycle rule 이 prefix 단위라 개별 제외가 안 되고, 재설계 필요로 남겨둔 상태 (`2026-07-08_clip_favorites.sql` 주석 참고).

**→ 앱측 반영 포인트 (lifecycle 유무와 무관하게 필수)**:

- "즐겨찾기 클립 선택" 화면에서 `GET /clips/{id}/url` 404, 또는 presigned URL 재생/다운로드 실패(R2 404)를 **정상 케이스로 처리** (예: "만료된 클립" 표시 + 선택 불가).
- 글 작성 플로우의 다운로드 단계도 동일 — 실패 시 사용자에게 만료 안내.

## 4. §5-④ 아바타 버킷 → 앱 팀 소유 영역

`user_profiles`·아바타 버킷 모두 앱 팀 소유라 terra-server 레포에는 정보가 없음. 앱 팀이 자체 버킷 정책을 확인해서 판단할 사안이고, 문서에 적힌 대로 **이니셜 폴백으로 시작**하는 게 안전해 보임.

---

## 5. DDL 리뷰 코멘트 (충돌 아님 — 참고용)

| # | 항목 | 내용 |
|---|---|---|
| 1 | `posts_insert` 경로 검증 없음 | `video_path` 가 본인 폴더인지 RLS 로 검증 안 함 → 남의 `{user_id}/posts/...` 경로를 가리키는 글 작성이 가능. 버킷 읽기가 어차피 로그인 유저 전체라 실해는 작지만, 앱에서 경로를 `auth.uid()` 기준으로 생성하면 충분. 인지만 해둘 것. |
| 2 | `community_reports.target_id` FK 없음 | polymorphic(post/comment 겸용) 의도로 보임 — 문제 없음. |
| 3 | 댓글 UPDATE 정책 없음 | 수정 불가·삭제만 가능. MVP 의도로 보임 — 문제 없음. |
| 4 | `public_profiles` SECURITY DEFINER 뷰 | 노출 컬럼 3개로 못박은 의도적 설계라는 점 확인. Supabase advisor lint 경고는 무시해도 됨. |

---

## 6. 부탁 사항 (1건)

같은 Supabase 프로젝트를 공유하므로, **실행한 DDL 사본을 커밋해두고 실행 완료 시점을 알려줘요.** terra-server 쪽에서 나중에 스키마를 조사할 때 "출처 불명 객체"로 보이지 않도록, terra-server `migrations/` 관례(`YYYY-MM-DD_*.sql`)처럼 앱 레포에 날짜 파일로 남기면 충분합니다. 실행 완료 통보를 받으면 terra-server 문서에도 "앱 팀 소유 객체 목록"으로 한 줄 기록해두겠음.

## 7. 진행 순서 (갱신)

1. ~~이 문서 회신~~ ← **본 문서. §5 4건 회신 완료, 마이그레이션 진행 OK**
2. terra-server: 프로덕션에서 `check_r2_lifecycle.py` 실행 → 30일 rule 실제 여부 공유 (§3 후속)
3. 앱 팀: 마이그레이션 실행 → DDL 사본 커밋 + 완료 통보 (§6)
4. 앱 1차(MVP) 구현 — 만료 클립 실패 처리 포함 (§3)
5. 2차(공지·신고/차단·모아보기·자동재생) → 스토어 제출 전 Apple 1.2 확인
