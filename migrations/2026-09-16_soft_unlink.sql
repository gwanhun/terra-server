-- 2026-09-16: 기기 소프트 해제(unlink) — devices/cameras.unlinked_at
--
-- 배경 (앱 회신 2026-09-16 §1, 재설계 회신 §2.5):
--   앱의 "기기 삭제" 는 등록 해제일 뿐 과거 기록(센서·명령·알림·영상)을 지우면 안 된다.
--   그런데 DELETE /devices|cameras/{id} 는 hard delete + cascade 라 전부 사라진다.
--   → 행을 지우지 않고 unlinked_at 만 찍는 소프트 해제를 신설한다.
--
-- 해제 시 서버가 하는 일 (backend/routers/devices.py · cameras.py `unlink_*`):
--   1. unlinked_at = now(), enclosure_id = NULL  (행은 보존)
--   2. 해당 기기의 schedules.enabled = false     (해제된 기기의 예약이 실행되면 안 됨)
--   3. Mosquitto 계정 회수                       (이후 접속 거부)
--   4. unlink_request_id 저장                    (멱등 — 같은 request_id 재시도는 동일 응답)
--
-- 보존되는 것: devices/cameras 행, telemetry*, commands, alerts, schedules(비활성),
--             motion_clips, clip_favorites, R2 원본.
-- 조회: GET /devices · GET /cameras 는 unlinked_at IS NULL 만 반환.
--       앱의 Supabase 직결 SELECT 는 앱이 unlinked_at 을 보고 거른다 (RLS 변경 없음).
-- 재등록: 같은 하드웨어가 다시 pair 하면 새 행이 생기고 이 행은 unlinked_at 상태로 남는다.
--         (페어링 멱등성은 하드웨어 ID 펌웨어 과제와 함께 별도)
--
-- hard delete(DELETE /devices|cameras/{id})는 운영·탈퇴용으로 남기며 앱은 호출하지 않는다.
--
-- 선행 마이그레이션: 2026-05-26_initial_schema.sql, 2026-05-26_camera_schema.sql


-- =====================================================================
-- 1. 컬럼
-- =====================================================================

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS unlinked_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS unlink_request_id  UUID;

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS unlinked_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS unlink_request_id  UUID;

COMMENT ON COLUMN public.devices.unlinked_at
    IS '소프트 해제 시각. NULL = 활성. 행·기록은 보존 (앱 2026-09-16 §1)';
COMMENT ON COLUMN public.devices.unlink_request_id
    IS '해제 요청의 앱 생성 UUID. 같은 값 재시도는 동일 응답(멱등)';
COMMENT ON COLUMN public.cameras.unlinked_at
    IS '소프트 해제 시각. NULL = 활성. motion_clips·R2 는 보존';
COMMENT ON COLUMN public.cameras.unlink_request_id
    IS '해제 요청의 앱 생성 UUID. 같은 값 재시도는 동일 응답(멱등)';


-- =====================================================================
-- 2. 활성 기기 조회용 부분 인덱스
-- =====================================================================
-- 목록 조회(owner_id 기준)와 브리지의 device_id → uuid 해상이 전부 "활성만" 을 본다.

CREATE INDEX IF NOT EXISTS idx_devices_owner_active
    ON public.devices (owner_id) WHERE unlinked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_cameras_owner_active
    ON public.cameras (owner_id) WHERE unlinked_at IS NULL;


-- =====================================================================
-- 적용 확인
-- =====================================================================
--   SELECT count(*) FILTER (WHERE unlinked_at IS NULL)     AS active,
--          count(*) FILTER (WHERE unlinked_at IS NOT NULL) AS unlinked
--     FROM public.devices;
