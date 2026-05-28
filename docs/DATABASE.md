# DATABASE 스키마

> 마이그레이션:
> - [migrations/2026-05-26_initial_schema.sql](../migrations/2026-05-26_initial_schema.sql) (IoT)
> - [migrations/2026-05-26_camera_schema.sql](../migrations/2026-05-26_camera_schema.sql) (카메라/영상)

## 테이블 9개

| 테이블 | 도메인 | 용도 | 키 |
|--------|--------|------|---|
| `enclosures` | 공통 | 사육장 (상위 묶음) | UUID, owner_id FK |
| `devices` | IoT | ESP32-S3 등록 | UUID, owner_id + enclosure_id FK |
| `device_settings` | IoT | 사용자별 목표/임계값 | device_id PK (1:1) |
| `telemetry` | IoT | 시계열 원본 (3초) | (device_id, ts) |
| `telemetry_1m` | IoT | 분 단위 다운샘플 | (device_id, bucket) |
| `commands` | IoT | 명령 이력 | UUID, device_id FK |
| `alerts` | IoT | 알림 이력 | UUID, device_id FK |
| `cameras` | 영상 | 카메라 워커 등록 (ESP32-P4 / RPi) | UUID, owner_id + enclosure_id FK |
| `motion_clips` | 영상 | 모션 영상 메타 (R2 키, H.264 mp4) | UUID, camera_id FK |

## ERD (ASCII)

```
auth.users (Supabase Auth)
   │
   │ owner_id
   ▼
┌────────────┐
│ enclosures │
└────┬───────┘
     │
     ├──► devices  (1:N)
     │     │
     │     ├──► device_settings (1:1)
     │     ├──► telemetry         (시계열, 7일)
     │     ├──► telemetry_1m      (집계, 1년)
     │     ├──► commands          (명령 이력)
     │     └──► alerts            (알림 이력)
     │
     └──► cameras  (ESP32-P4 / RPi 워커, 1:N)
           │
           └──► motion_clips (R2 메타, H.264 mp4)
                      │
                      └──► (R2 객체: r2_key, e.g. clips/2026/.../p4cam-xxxx/.mp4)
```

**enclosure 없이 단독 디바이스/카메라도 가능** (devices.enclosure_id, cameras.enclosure_id 가 NULL 허용).

## RLS 정책

| 테이블 | SELECT | INSERT | UPDATE | DELETE |
|--------|--------|--------|--------|--------|
| `enclosures` | 본인 | 본인 | 본인 | 본인 |
| `devices` | 본인 | **service_role only** (페어링 API) | 본인 | 본인 |
| `device_settings` | 본인 | 본인 | 본인 | 본인 |
| `telemetry` | 본인 | service_role only (bridge) | - | - |
| `telemetry_1m` | 본인 | service_role only | - | - |
| `commands` | 본인 | 본인 (issued_by=auth.uid()) | service_role only (status 갱신) | - |
| `alerts` | 본인 | service_role only (bridge) | 본인 (resolved_at) | - |
| `cameras` | 본인 | **service_role only** (페어링 API) | 본인 | 본인 |
| `motion_clips` | 본인 | service_role only (terra-api 업로드 콜백) | - | 본인 |

> "본인" = `auth.uid() = owner_id` 또는 cascade로 매핑

## service_role vs anon 키

- **백엔드 (`service_role`)** — RLS 바이패스, terra-api / terra-bridge 사용
  - **명시적 `.eq("owner_id", user_id)` 필터 필수**
- **앱 (`anon` + JWT)** — RLS 적용, auth.uid() 기반 자동 필터
- **디바이스/카메라 (페어링 후)** — service_role 안 씀. terra-api Bearer 토큰 인증 후 service_role 로 INSERT

## 마이그레이션 정책

- 파일명: `migrations/YYYY-MM-DD_<설명>.sql`
- 적용: Supabase 대시보드 > SQL Editor 에서 통째로 실행 (수동)
- 적용 순서: **파일명 날짜순 + 같은 날짜면 알파벳순**
  - 2026-05-26_initial_schema.sql (먼저)
  - 2026-05-26_camera_schema.sql (다음)

## 시계열 관리 정책

- **`telemetry` 원본**: 7일 보관 (pg_cron 자동 DELETE, Stage E)
- **`telemetry_1m`**: 1년 보관 (pg_cron 매분 INSERT, Stage E)

## 영상 보관 정책

- **`motion_clips` 메타**: 영구 보관 (R2 키만 저장, 가벼움)
- **R2 영상 파일**: **30일 lifecycle rule 로 자동 삭제** (Stage F2)
  - DB row 도 동기 삭제 (cron 또는 r2 lifecycle 이벤트 webhook)
- 사용자가 보존하고 싶은 클립은 별도 "favorite" 플래그 추가 (Stage F3)

### 영상 크기 (참고)

H.264 / HD 720p / 24fps / 10초 기준:
- 평균 **~1MB / 클립** (500KB ~ 1.5MB 변동)
- 디바이스 1대 × 일 100 클립 = ~100MB/day = ~3GB/월
- 30일 누적 = ~3GB → R2 무료 10GB 한도 안

ESP32-CAM MJPEG 대비 5~10배 축소.

## Realtime 구독 대상

`supabase_realtime` publication 에 추가된 테이블:
- `devices`, `telemetry`, `commands`, `alerts` (IoT)
- `enclosures`, `cameras`, `motion_clips` (영상)

→ 앱이 사육장 화면에서 센서값 + 새 모션 영상 + 명령 결과 + 알림을 모두 실시간 push 수신.
