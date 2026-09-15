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
| ⬜ | `2026-09-15_push_outbox.sql` | — | 앱 푸시 이벤트 전송 대기열 + 정리 cron. **미적용 — SQL Editor 실행 필요** |

## 규칙
- 새 마이그레이션은 `migrations/YYYY-MM-DD_설명.sql` 로 추가하고, 적용 후 이 표에 행 추가.
- 모든 SQL 은 `IF NOT EXISTS` 등으로 **재실행 안전(idempotent)** 하게 작성.
- 앱/펌웨어 계약과 연동되는 변경은 커밋 메시지에 `앱 §N` 표기.
