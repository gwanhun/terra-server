-- 2026-10-01: camera_logs — 카메라 펌웨어 에러 로그(ESP_LOGE) 1줄 1행
--
-- 출처: 펌웨어 하트비트 `errs` 배열 (app_errlog.c, noinit 링버퍼 → 15초 하트비트에 신규분만).
-- 목적: 시리얼 없이 "멈춤·재부팅 직전에 무슨 에러가 났는지" 를 콘솔에서 본다.
--       noinit 이라 재부팅 전 줄도 재부팅 후 첫 하트비트에 prev=true 로 올라온다.
-- 조회: service_role(백엔드)만. API GET /cameras/{uuid}/logs 가 소유자 확인 후 반환.
-- 보존: 카메라당 최근 14일(핸들러가 INSERT 때 오래된 행 삭제).

CREATE TABLE IF NOT EXISTS public.camera_logs (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   UUID NOT NULL REFERENCES public.cameras(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    uptime_s    INTEGER,                 -- 발생 시각(부팅 후 초)
    prev_boot   BOOLEAN NOT NULL DEFAULT false,  -- 재부팅 전 세대의 줄
    count       INTEGER NOT NULL DEFAULT 1,      -- 같은 줄 연속 반복 횟수
    msg         TEXT NOT NULL            -- "E (12345) tag: message" 원문(ANSI 제거)
);

CREATE INDEX IF NOT EXISTS camera_logs_camera_created_idx
    ON public.camera_logs (camera_id, created_at DESC);

ALTER TABLE public.camera_logs ENABLE ROW LEVEL SECURITY;
-- 정책 없음 = anon/authenticated 는 접근 불가, service_role 만.

COMMENT ON TABLE public.camera_logs IS '카메라 펌웨어 ESP_LOGE 줄(하트비트 errs). prev_boot=true 는 재부팅 전 줄.';
