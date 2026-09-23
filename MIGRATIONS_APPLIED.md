# MIGRATIONS_APPLIED

Supabase 에 적용한 마이그레이션 기록(SOT). `migrations/*.sql` 을 SQL Editor 에서 실행한 뒤
여기에 체크한다. 파일명 날짜 = 작성일, 아래 "적용일" = 실제 DB 반영일.

| 적용 | 파일 | 적용일 | 내용 |
|:---:|---|---|---|
| ✅ | `2026-05-26_initial_schema.sql` | 2026-05-26 | 초기 스키마 (devices/device_settings/telemetry/commands/alerts …) |
| ✅ | `2026-05-26_camera_schema.sql` | 2026-05-26 | 카메라/클립 스키마 |
| ✅ | `2026-06-30_telemetry_30m_pgcron.sql` | 2026-06-30 | telemetry 30분 롤업 pg_cron |
| ✅ | `2026-07-08_clip_favorites.sql` | 2026-07-08 | 클립 즐겨찾기 |
| ✅ | `2026-08-06_clip_purpose.sql` | 2026-08-06 | motion_clips 촬영 목적 |
| ✅ | `2026-08-10_schedules.sql` | 2026-08-10 | 예약 타이머 + guard/next_run_at |
| ✅ | `2026-08-12_commands_source_and_guard.sql` | 2026-08-12 | commands source/source_id/reason |
| ✅ | `2026-08-18_devices_capabilities.sql` | 2026-08-18 | §2 `devices.capabilities` JSONB (기존 행 relay 백필) |
| ✅ | `2026-08-18_device_settings_setpoint.sql` | 2026-08-18 | §5 `device_settings.target_temp_c` / `target_humidity_pct` |
| ✅ | `2026-08-18_schedules_pair_id.sql` | 2026-08-18 | §3 `schedules.pair_id` + 부분 인덱스 |
| ✅ | `2026-08-18_telemetry_led.sql` | 2026-08-18 | §4 `telemetry.led` + `led_brightness` |
| ✅ | `2026-09-07_telemetry_fan2.sql` | 2026-09-07 | `telemetry.fan2` (냉각팬 상태) |
| ✅ | `2026-09-08_cameras_rotate_capabilities.sql` | 2026-09-15 | 카메라 `rotate_180` + `capabilities` (앱 핸드오프 rotate180 R4/R5) |
| ✅ | `2026-09-15_telemetry_30m_valid_counts.sql` | 2026-09-15 | `telemetry_30m` 지표별 유효 표본 수 + 센서 fault 집계 제외 (cron `downsample-telemetry-30m` jobid=1 덮어씀) |
| ✅ | `2026-09-15_push_outbox.sql` | 2026-09-16 | 앱 푸시 이벤트 전송 대기열 + 정리 cron (`cleanup-push-outbox` jobid=6 신규) |
| ✅ | `2026-09-16_enclosures_name_unique.sql` | 2026-09-16 | 그룹 이름 중복 금지 UNIQUE 인덱스 (앱 §1-1: btrim, 대소문자 구분). 생성 성공 = 기존 중복 없음 |
| ✅ | `2026-09-16_soft_unlink.sql` | 2026-09-16 | devices/cameras `unlinked_at` + `unlink_request_id` + 활성 부분 인덱스 (소프트 해제, 앱 §1) |
| ⬜ | `2026-09-17_cameras_image_state.sql` | — | `cameras.image_state` (노출/야간 상태 텔레메트리). **미적용** |
| ✅ | `2026-09-16_cameras_clip_stats.sql` | 2026-09-16 | `cameras.clip_stats` JSONB + `clip_stats_at` (펌웨어 텔레메트리 `clips` 카운터) |
| ✅ | `2026-09-16_app_redesign_01_assignment_history.sql` | 2026-09-16 | 앱 재설계 번들 (tera-ai-flutter@bb430fa). pets.deleted_at · pet_camera_assignments 이력 테이블 · reconcile 헬퍼 · pets_user_id_fkey CASCADE 교체 |
| ✅ | `2026-09-16_app_redesign_02_redesign_groups.sql` | 2026-09-16 | 앱 재설계 번들 (tera-ai-flutter@bb430fa). 그룹 RPC(save/remove/rename) · 이름 검증 · 멱등 요청·카운터 테이블 · enclosures.group_number |
| ✅ | `2026-09-16_app_redesign_03_relationship_triggers.sql` | 2026-09-16 | 앱 재설계 번들 (tera-ai-flutter@bb430fa). cameras/pets AFTER UPDATE 트리거 → reconcile (서버·웹·REST 경로 이력 커버) |
| ✅ | `2026-09-16_app_redesign_04_redesign_pets.sql` | 2026-09-16 | 앱 재설계 번들 (tera-ai-flutter@bb430fa). 개체 저장/툼스톤 RPC · 활성 개체 RESTRICTIVE SELECT 정책 |
| ✅ | `2026-09-16_app_redesign_05_redesign_delete_group.sql` | 2026-09-16 | 앱 재설계 번들 (tera-ai-flutter@bb430fa). 그룹 원자 삭제 RPC (구성원·영상·이력 보존) |
| ✅ | `2026-09-16_app_redesign_06_clip_visibility.sql` | 2026-09-16 | 앱 재설계 번들 (tera-ai-flutter@bb430fa). user_hidden_clips (앱 전용 영상 숨김) |
| ✅ | `2026-09-21_gme_jobs_clip_cascade.sql` | 2026-09-21 | `gme_jobs.clip_id` FK 에 ON DELETE CASCADE. 카메라 삭제 500 대응이었으나 **단독으로는 불충분** — motion_clips 참조 테이블이 45개라 클립 보유 기기는 소프트 해제 사용 |
| ✅ | `2026-09-21_cameras_hw_id.sql` | 2026-09-21 | `cameras.hw_id`(efuse MAC) + `(owner_id, hw_id)` 부분 UNIQUE. 재페어링 시 기존 행 재사용 → 중복 카메라 행 방지 |
| ✅ | `2026-09-21_devices_hw_id.sql` | 2026-09-21 | `devices.hw_id` 동일 적용. **코드만 먼저 배포되어 20:44~20:46 페어링이 PostgREST 400 → `PAIR_FAIL 500` 으로 실패했었음** (컬럼 없는 상태에서 조회·INSERT) |
| ✅ | `20260922_redesign_conflict_errcode.sql` (**앱 레포**) | 2026-09-22 | **이 레포에 파일 없음.** 앱팀이 운영 DB 에 직접 적용 — `redesign_save_group_v1`·`redesign_remove_group_member_v1`·`redesign_save_pet_v1`·`redesign_delete_pet_v1` 의 '구성 변경' 오류를 `40001` → `PT409`. 40001 은 PostgREST 가 무한 재시도해 24시간 520만 건·연결 4개 점유를 유발했음. 권위 있는 SQL = `tera-ai-flutter@main supabase/migrations/20260922_redesign_conflict_errcode.sql`. 이 레포 사본 `_02_`·`_04_` 는 **2026-09-23 동기화 완료**(7곳 `40001`→`PT409` 치환, 원문 수령 후) — 재적용해도 운영과 일치 |
| ✅ | `2026-09-22_webrtc_connect_logs.sql` | 2026-09-23 | `webrtc_connect_logs` (라이브 연결 시도 1건당 1행, 앱 직접 INSERT·service_role 조회). 앱 핸드오프 2026-09-22 요청 1. 보존 cron `cleanup-webrtc-connect-logs-90d` **jobid=10** 등록 확인 |

## 규칙
- 새 마이그레이션은 `migrations/YYYY-MM-DD_설명.sql` 로 추가하고, 적용 후 이 표에 행 추가.
- 모든 SQL 은 `IF NOT EXISTS` 등으로 **재실행 안전(idempotent)** 하게 작성.
- 앱/펌웨어 계약과 연동되는 변경은 커밋 메시지에 `앱 §N` 표기.
- **앱팀이 운영 DB 를 직접 고친 경우에도 이 표에 ⚠️ 행으로 남긴다.** 파일이 이 레포에 없어도
  기록이 없으면 우리 사본을 재적용하다 조용히 되돌린다 (2026-09-22 PT409 사례).
- **새 RPC 에서 충돌·구성 변경류 오류는 `40001`/`40P01` 대신 `PT409` 를 쓴다.**
  PostgREST 가 40001 을 일시적 충돌로 보고 자동 재시도하는데, 구성 불일치는 재시도해도
  조건이 안 바뀌어 무한 루프가 된다.
- **컬럼을 쓰는 코드는 마이그레이션 적용 후에 배포한다.** 순서가 뒤집히면 PostgREST 가 400 을 돌려주고
  기기에는 `PAIR_FAIL 500` 으로 보인다 (2026-09-21 `devices.hw_id` 사례).
