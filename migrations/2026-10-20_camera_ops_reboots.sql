-- 2026-10-20: 운영 카메라 재부팅 기록 (petcam-lab 요청 6, specs/stage-m-ops-camera-reboot.md)
-- POST /ops/cameras/{id}/reboot 발행 시도마다 1행 — 발행 전에 INSERT(published=NULL) 하고 결과로 UPDATE.
-- 쿨다운 판정·베타 리포트 집계·ack 확인용. FK 없음: 카메라 행이 지워져도 기록 보존(ota_jobs 관례).
-- RLS 켜고 정책 없음 = service_role 전용 (앱·anon 읽기 불가).
-- **코드 배포 전에 적용할 것** — 없으면 API 가 INSERT 실패로 503(재부팅 안 함). 적용 후 MIGRATIONS_APPLIED.md 기록.

CREATE TABLE IF NOT EXISTS public.camera_ops_reboots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id     UUID NOT NULL,                  -- cameras.id
    msg_id        UUID NOT NULL UNIQUE,           -- 발행한(하려던) reboot 명령 msg_id
    reason        TEXT NOT NULL,                  -- 예: night_rec_stall
    requested_by  TEXT NOT NULL,                  -- 예: beta-camera-watch
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published     BOOLEAN,                        -- NULL=발행 중, true=브로커 발행 성공, false=실패. 쿨다운은 false 아닌 행
    ack_result    TEXT,                           -- 카메라 ack result (ok | rejected_* ...)
    acked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS camera_ops_reboots_camera_requested_idx
    ON public.camera_ops_reboots (camera_id, requested_at DESC);

ALTER TABLE public.camera_ops_reboots ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.camera_ops_reboots FROM anon, authenticated;

COMMENT ON TABLE public.camera_ops_reboots IS
    '운영 재부팅(서버 시크릿 경로) 기록. 앱에 노출하지 않음. backend/routers/ops.py';
