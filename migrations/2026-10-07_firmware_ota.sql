-- 2026-10-07: 펌웨어 원격 업데이트(OTA) — Stage J (specs/stage-j-ota.md)
--
-- firmware_releases : 운영자가 scripts/upload_firmware.py 로 등록한 빌드 1건 = 1행.
--                     바이너리 본문은 R2 (r2_key), 서버는 토큰 인증 뒤 스트리밍(프록시)한다.
-- ota_jobs          : 기기 1대에 릴리스 1건을 적용하는 작업. 2단계(prepare → apply).
--
-- 상태 전이 (backend/ota_service.py 가 SOT):
--   pending     : 작업 생성됨, 명령 발행 전/직후
--   accepted    : 펌웨어가 ota_prepare 를 ack ok 로 받음(예약)
--   downloading : 진행률 ack 수신 중 (pct)
--   ready       : 다운로드·검증 완료, 비활성 슬롯에 기록됨. 부팅 파티션은 아직 그대로
--   applying    : ota_apply 발행됨 → 재부팅 중
--   verified    : 새 펌웨어 heartbeat `fw` == release.version
--   failed      : 펌웨어 보고 실패 / 구 펌웨어 거부 / 발행 실패 (error 에 사유)
--   rolled_back : apply 후 heartbeat `fw` 가 이전 버전으로 돌아옴(부트로더 롤백)
--   timeout     : 단계별 시한 초과 (ota_service.OtaMonitor)
--
-- 조회: service_role(백엔드)만. 앱 노출 없음(Out). 콘솔은 JWT API 경유.

CREATE TABLE IF NOT EXISTS public.firmware_releases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target      TEXT NOT NULL CHECK (target IN ('camera_p4', 'device_nano')),
    version     TEXT NOT NULL,                    -- esp_app_desc.version == APP_FIRMWARE_VER (예 "fb2-p4 0.2.2-20261010")
    r2_key      TEXT NOT NULL,                    -- firmware/<target>/<version>.bin
    elf_r2_key  TEXT,                             -- 코어덤프 해석용 ELF (선택)
    size_bytes  INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    project_name TEXT,                            -- esp_app_desc.project_name (타겟 교차 검증)
    idf_ver     TEXT,
    notes       TEXT,
    created_by  UUID,                             -- 업로드한 운영자 (auth.users.id, 없을 수 있음)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (target, version)
);

CREATE INDEX IF NOT EXISTS firmware_releases_target_created_idx
    ON public.firmware_releases (target, created_at DESC);

ALTER TABLE public.firmware_releases ENABLE ROW LEVEL SECURITY;
-- 정책 없음 = service_role 만.

COMMENT ON TABLE public.firmware_releases IS 'OTA 릴리스. 본문은 R2, 서버가 토큰 인증 후 프록시. UNIQUE(target, version)';


CREATE TABLE IF NOT EXISTS public.ota_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          TEXT NOT NULL CHECK (kind IN ('camera', 'device')),
    target_uuid   UUID NOT NULL,                  -- cameras.id | devices.id (kind 로 구분, FK 없음: 소프트 해제 행도 이력 보존)
    release_id    UUID NOT NULL REFERENCES public.firmware_releases(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','accepted','downloading','ready','applying',
                                    'verified','failed','rolled_back','timeout')),
    pct           SMALLINT NOT NULL DEFAULT 0 CHECK (pct BETWEEN 0 AND 100),
    prepare_msg_id UUID,                          -- ota_prepare 의 msg_id (카메라: 직접 발행 / 기기: commands.id)
    apply_msg_id  UUID,                           -- ota_apply 의 msg_id
    prev_version  TEXT,                           -- 작업 시작 시점의 firmware_ver (롤백 판정 기준)
    error         TEXT,
    issued_by     UUID,                           -- 트리거한 사용자
    forced        BOOLEAN NOT NULL DEFAULT false, -- 사전 점검 게이트를 건너뛴 작업(개발용)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at    TIMESTAMPTZ,                    -- ota_apply 발행 시각
    finished_at   TIMESTAMPTZ                     -- 종결 상태 진입 시각
);

CREATE INDEX IF NOT EXISTS ota_jobs_target_created_idx
    ON public.ota_jobs (target_uuid, created_at DESC);
-- 진행 중 작업 스윕·중복 방지용 (종결 상태는 제외)
CREATE INDEX IF NOT EXISTS ota_jobs_active_idx
    ON public.ota_jobs (status)
    WHERE status IN ('pending','accepted','downloading','ready','applying');

ALTER TABLE public.ota_jobs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.ota_jobs IS 'OTA 작업 1건(기기 1대 × 릴리스 1건). 2단계 prepare→apply. 상태 전이는 backend/ota_service.py';
