-- 2026-10-08 Stage J OTA: 릴리스 퇴역(retire) 컬럼
--
-- 왜: ota_jobs.release_id 가 firmware_releases 를 NOT NULL FK 로 참조해서, 한 번이라도 작업에
-- 쓰인 릴리스는 행을 지울 수 없다(작업 이력이 깨짐). 불량으로 판명된 빌드(예: 카메라 0.3.1/0.3.2 —
-- prepare 스택 오버플로)는 "더는 OTA 대상으로 못 고르게" 만들어야 하므로 soft-delete 로 처리한다.
--   * retired_at 이 NULL 이 아니면: GET /firmware/releases 기본 목록에서 제외, POST …/ota 409,
--     GET /firmware/jobs/{id}/bin 410. R2 바이너리는 scripts/upload_firmware.py --delete 가 지운다.
--   * 작업 이력이 없는 릴리스는 같은 스크립트가 행을 바로 DELETE 한다(이 컬럼 안 씀).
-- 재실행 안전.

ALTER TABLE public.firmware_releases
    ADD COLUMN IF NOT EXISTS retired_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retired_reason TEXT;

COMMENT ON COLUMN public.firmware_releases.retired_at IS
    'OTA 대상에서 제외된 시각(soft-delete). 작업 이력(ota_jobs FK) 때문에 행은 남긴다';
COMMENT ON COLUMN public.firmware_releases.retired_reason IS
    '퇴역 사유 (upload_firmware.py --delete --reason)';
