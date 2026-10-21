# DATABASE 스키마

> 마이그레이션:
> - [migrations/2026-05-26_initial_schema.sql](../migrations/2026-05-26_initial_schema.sql) (IoT)
> - [migrations/2026-05-26_camera_schema.sql](../migrations/2026-05-26_camera_schema.sql) (카메라/영상)
> - [migrations/2026-10-20_live_recordings.sql](../migrations/2026-10-20_live_recordings.sql) (라이브 직접 녹화)
> - [migrations/2026-10-20_camera_ops_reboots.sql](../migrations/2026-10-20_camera_ops_reboots.sql) (운영 카메라 재부팅 기록)

## 테이블 9개

| 테이블 | 도메인 | 용도 | 키 |
|--------|--------|------|---|
| `enclosures` | 공통 | 사육장 (상위 묶음) | UUID, owner_id FK |
| `devices` | IoT | ESP32-S3 등록. `sys_state`(진단 최신값, 2026-09-28) + `temp_offset_c`(지금 적용 중인 온도 보정, 펌웨어 보고, NULL=구 펌웨어 — 2026-10-01) + `lcd_text`/`lcd_text_updated_at`(지금 LCD 문구 원문, 기기 ACK ok 시 확정, NULL=기본값·미상 — 2026-10-07) | UUID, owner_id + enclosure_id FK |
| `device_settings` | IoT | 사용자별 목표/임계값 | device_id PK (1:1) |
| `telemetry` | IoT | 시계열 원본 (3초) | (device_id, ts) |
| `telemetry_1m` | IoT | 분 단위 다운샘플 | (device_id, bucket) |
| `commands` | IoT | 명령 이력 | UUID, device_id FK |
| `alerts` | IoT | 알림 이력 | UUID, device_id FK |
| `cameras` | 영상 | 카메라 워커 등록 (ESP32-P4 / RPi). `rotate_180`(180° 회전 설정, 진실) + `capabilities`(펌웨어 보고, NULL=구 펌웨어) — 2026-09-08. `device_id`(Terra Hub 짝 devices 행, NULL=순수 카메라) — 2026-10-06 | UUID, owner_id + enclosure_id FK |
| `motion_clips` | 영상 | 모션 영상 메타 (R2 키, H.264 mp4) | UUID, camera_id FK |
| `live_recordings` | 영상 | 앱이 라이브 화면에서 직접 녹화한 영상(≤120초 mp4 + jpg, 2026-10-21 60→120초) — 2026-10-20. `status` pending→ready, `client_id`(앱 멱등 키), `r2_key`/`thumbnail_key`(`terra-clips/recordings/{user_id}/{camera_id}/{id}.mp4·.jpg`), `has_thumbnail`(생성 컬럼 = `thumbnail_key is not null`, INSERT 금지). 계정당 50개(pending+ready), 24시간 넘은 pending 은 서버 스레드가 행·객체 삭제. 보관 무기한 | UUID, user_id FK(auth.users, cascade), camera_id FK(cameras, **on delete set null**), UNIQUE(user_id, client_id) |
| `firmware_releases` | OTA | 펌웨어 빌드 1건(`target` camera_p4/device_nano/hub_p4(2026-10-14, 허브 model=esp32-p4-hub 전용), `version`=esp_app_desc, R2 키, sha256). `scripts/upload_firmware.py` 가 INSERT — 2026-10-07. `retired_at`/`retired_reason`(2026-10-08) = 퇴역(soft-delete): 목록·OTA 대상·다운로드 제외, `ota_jobs` FK 이력 보존 | UUID, UNIQUE(target, version) |
| `ota_jobs` | OTA | 기기 1대 × 릴리스 1건 작업. `status` pending→accepted→downloading→ready→applying→verified/rolled_back, failed/timeout. `prepare_msg_id`/`apply_msg_id`(카메라: MQTT msg_id, 기기: commands.id) — 2026-10-07 | UUID, release_id FK, target_uuid(FK 없음: 해제 행 이력 보존) |
| `camera_ops_reboots` | 운영 | 운영 재부팅(`POST /ops/cameras/{id}/reboot`, petcam 감시 스크립트) 발행 시도 1건 = 1행 — 2026-10-20. 발행 전 INSERT(`published=NULL`) → 결과로 `published` true/false UPDATE. `ack_result`/`acked_at` 은 브리지가 카메라 ack 로 채움. 쿨다운(2시간)은 `published` 가 false 아닌 행 기준. 건너뜀(409)은 기록 안 함. 앱 비노출 | UUID, camera_id(FK 없음: 카메라 삭제 후에도 기록 보존), UNIQUE(msg_id), INDEX(camera_id, requested_at desc) |

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

**Terra Hub**(2026-10-06): 카메라+센서 통합 보드 1대 = `cameras` 행 1 + `devices` 행 1. 같은 텍스트 id(`p4hub-…`)·같은 `token_hash`, `cameras.device_id` 로 링크. 앱은 기존처럼 "카메라 1 + 기기 1" 로 본다 — 앱 변경 0. [specs/stage-k-unified-hub.md](../specs/stage-k-unified-hub.md)

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
| `live_recordings` | 본인 (`user_id = auth.uid()`) | service_role only (`POST /recordings`) | service_role only (complete) | service_role only (`DELETE /recordings/{id}`, 정리 스레드) |
| `camera_ops_reboots` | service_role only (정책 없음, anon·authenticated REVOKE) | service_role only (`/ops` 라우터) | service_role only (`/ops` 라우터, 브리지 ack) | - |

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

- **`telemetry` 원본**: 7일 보관 (pg_cron `cleanup-telemetry-7d` 자동 DELETE)
- **`telemetry_30m`**: 30분 집계, 삭제 cron 없음 → **사실상 영구**. 장기 차트의 정본
  - `sample_count` 는 버킷 내 **행 수**. 지표별 유효 표본 수는 `t_a_count` / `h_a_count` / `t_b_count` / `h_b_count`
    (`2026-09-15_telemetry_30m_valid_counts.sql` 이후 버킷부터, 그 전은 NULL·복원 불가)
  - 센서 fault(`a_ok`/`b_ok` false) 샘플은 집계에서 제외됨
- **`telemetry_1m`**: ⚠️ **미사용 — 채우는 cron 이 없어 영구히 빈 테이블** (Stage E 보류). 앱은 쓰지 말 것

## 영상 보관 정책

- **`motion_clips` 메타**: 영구 보관 (R2 키만 저장, 가벼움)
- **R2 영상 파일**: **30일 lifecycle rule 로 자동 삭제** (Stage F2)
  - DB row 도 동기 삭제 (cron 또는 r2 lifecycle 이벤트 webhook)
- 사용자가 보존하고 싶은 클립은 별도 "favorite" 플래그 추가 (Stage F3)
- **`live_recordings`**(2026-10-20): 무기한(사용자가 지울 때까지). R2 경로 `terra-clips/recordings/` 는 30일 lifecycle 대상이 **아니어야** 한다(배포 전 대시보드 확인 — 버킷을 petcam-lab 과 공유)

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
