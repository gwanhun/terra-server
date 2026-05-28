-- =====================================================================
-- terra-server: 카메라/모션 클립 스키마 (2026-05-26)
-- 적용 방법: Supabase 대시보드 > SQL Editor 에서 통째로 실행
--
-- 추가 도메인: ESP32-P4 (또는 RPi) 카메라 워커가 자체 모션 감지 →
-- H.264 mp4 영상(HD 720p 10초)을 R2 에 직접 PUT, 메타만 Supabase 에 저장.
-- terra-server 는 영상 분석을 하지 않는다.
--
-- 선행 마이그레이션: 2026-05-26_initial_schema.sql
-- =====================================================================


-- =====================================================================
-- 7. enclosures — 사육장 (상위 개념: ESP32 디바이스 + 카메라 묶음)
-- =====================================================================
-- 한 enclosure 가 하나의 사육장. 그 안에 센서/제어용 device, 카메라가 N개씩.
-- enclosure 없이 device 만 등록하는 것도 허용 (단독 사용 시나리오).
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.enclosures (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,              -- "거실 비어디드"
    species     TEXT,                        -- "bearded_dragon", "leopard_gecko"
    note        TEXT,                        -- 사용자 메모
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_enclosures_owner ON public.enclosures(owner_id);

CREATE TRIGGER trg_enclosures_updated_at
    BEFORE UPDATE ON public.enclosures
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


-- =====================================================================
-- devices 에 enclosure_id 추가 (기존 마이그레이션에서 생성된 테이블 확장)
-- =====================================================================
-- ON DELETE SET NULL: enclosure 삭제해도 device 자체는 보존 (재배정 가능).
-- =====================================================================

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS enclosure_id UUID REFERENCES public.enclosures(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_devices_enclosure ON public.devices(enclosure_id);


-- =====================================================================
-- 8. cameras — 카메라 워커 등록 (ESP32-P4 또는 RPi)
-- =====================================================================
-- devices 테이블과 구조 유사. 분리 이유:
--   - device(ESP32-S3) 는 액추에이터 제어 명령 받음, camera 는 영상 업로드 권한만
--   - MQTT ACL 분리 (camera 는 motion_event 토픽만 publish)
--   - 향후 IP 카메라 등 다른 종류 추가 가능성
-- token_hash 는 MQTT password + R2 presigned URL 발급 시 인증용.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.cameras (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    enclosure_id  UUID REFERENCES public.enclosures(id) ON DELETE SET NULL,
    camera_id     TEXT NOT NULL UNIQUE,        -- 카메라 워커의 MQTT client_id (e.g. "p4cam-a1b2c3d4" / "picam-...")
    token_hash    TEXT NOT NULL,                -- MQTT password bcrypt
    name          TEXT NOT NULL,                -- "거실 비어디드 카메라"
    model         TEXT DEFAULT 'esp32-p4',     -- 'esp32-p4' | 'rpi-zero-2-w' | 'rpi-4' | 'ip-camera' 등
    firmware_ver  TEXT,                          -- 워커 펌웨어 버전 (e.g. "terra-cam-p4 0.1.0")
    -- 캡처 설정 (워커가 부팅 시 조회)
    resolution    TEXT DEFAULT 'HD',            -- 'VGA' | 'HD' (720p) | 'FHD' (1080p)
    fps           INT  DEFAULT 24,
    clip_sec      INT  DEFAULT 10,              -- 모션 감지 시 캡처 길이
    -- 라이브 스트리밍 설정 (Stage G)
    stream_mode   TEXT,                          -- NULL | 'snapshot' | 'webrtc' (현재 활성 모드)
    stream_until  TIMESTAMPTZ,                  -- 스트리밍 자동 종료 시각 (예: 5분 후)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ,
    is_online     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_cameras_owner ON public.cameras(owner_id);
CREATE INDEX IF NOT EXISTS idx_cameras_enclosure ON public.cameras(enclosure_id);

CREATE TRIGGER trg_cameras_updated_at
    BEFORE UPDATE ON public.cameras
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


-- =====================================================================
-- 9. motion_clips — 모션 감지된 영상 메타
-- =====================================================================
-- 영상 파일 자체는 R2. DB 는 r2_key + 메타만.
-- enclosure_id 는 cameras 에서 cascade 로 조회 가능하지만 쿼리 단순화 위해 denormalize.
-- 보관 정책: 30일 후 자동 삭제 (R2 lifecycle rule + DB row 동기 삭제, Stage F2).
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.motion_clips (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id     UUID NOT NULL REFERENCES public.cameras(id) ON DELETE CASCADE,
    enclosure_id  UUID REFERENCES public.enclosures(id) ON DELETE SET NULL,
    owner_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    started_at    TIMESTAMPTZ NOT NULL,
    duration_sec  FLOAT NOT NULL,
    -- R2 객체 키 (e.g. "clips/2026/05/27/{camera_id}/{clip_id}.mp4")
    r2_key        TEXT NOT NULL,
    -- 썸네일 (첫 프레임 JPEG, 옵션)
    thumbnail_key TEXT,
    -- 파일 메타
    file_size     INT,           -- bytes (HD 10초 H.264 ~500KB~1.5MB)
    container     TEXT DEFAULT 'mp4',
    codec         TEXT DEFAULT 'h264',
    width         INT,
    height        INT,
    fps           FLOAT,
    -- 모션 감지 정보
    motion_score  FLOAT,         -- 0.0~1.0 (감지 시 강도, 옵션)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clips_camera ON public.motion_clips(camera_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_clips_enclosure ON public.motion_clips(enclosure_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_clips_owner_recent ON public.motion_clips(owner_id, started_at DESC);


-- =====================================================================
-- RLS 정책
-- =====================================================================

ALTER TABLE public.enclosures ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own enclosures all" ON public.enclosures
    FOR ALL USING (auth.uid() = owner_id)
    WITH CHECK (auth.uid() = owner_id);


ALTER TABLE public.cameras ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own cameras select" ON public.cameras
    FOR SELECT USING (auth.uid() = owner_id);
CREATE POLICY "own cameras update" ON public.cameras
    FOR UPDATE USING (auth.uid() = owner_id);
CREATE POLICY "own cameras delete" ON public.cameras
    FOR DELETE USING (auth.uid() = owner_id);
-- INSERT 는 RLS 없음 → 페어링 API (service_role) 만


ALTER TABLE public.motion_clips ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own clips select" ON public.motion_clips
    FOR SELECT USING (auth.uid() = owner_id);
CREATE POLICY "own clips delete" ON public.motion_clips
    FOR DELETE USING (auth.uid() = owner_id);
-- INSERT/UPDATE 는 service_role (terra-server 업로드 완료 콜백) 만


-- =====================================================================
-- Realtime publication
-- =====================================================================
-- 앱이 motion_clips 신규 추가를 실시간으로 받음 (사육장 화면에서 즉시 표시).
-- =====================================================================

ALTER PUBLICATION supabase_realtime ADD TABLE public.enclosures;
ALTER PUBLICATION supabase_realtime ADD TABLE public.cameras;
ALTER PUBLICATION supabase_realtime ADD TABLE public.motion_clips;
