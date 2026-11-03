-- 2026-11-02: 일일 온습도 분석 카드 (기획 v4 2-1~2-3, specs/stage-p-daily-analysis-v4.md Task 2)
--
-- 브리지(analysis_runner, service_role)가 매일 08시대에 1차, 09:05 이후 2차로 기기별 카드를 upsert.
-- 앱은 RLS 로 본인 기기 행만 SELECT + Realtime 구독. INSERT/UPDATE 정책이 없어 authenticated 는 쓰기 거부.
-- 해제 후 재페어링은 새 devices 행을 만들므로(routers/devices.py) 이전 소유자 리포트가 새 소유자에게 안 보인다.

CREATE TABLE IF NOT EXISTS public.env_daily_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    report_date   DATE NOT NULL,                 -- 집계 시작일(KST): report_date 08:00 ~ 다음 날 08:00
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    temp_card     JSONB,                         -- Card.to_json() | NULL = 미생성
    humid_card    JSONB,
    pass          SMALLINT NOT NULL DEFAULT 1,   -- 1 = 08시대 1차, 2 = 09:05 이후 재계산
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, report_date)
);
CREATE INDEX IF NOT EXISTS idx_env_daily_reports_device ON public.env_daily_reports(device_id, report_date DESC);
ALTER TABLE public.env_daily_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own env_daily_reports select" ON public.env_daily_reports;
CREATE POLICY "own env_daily_reports select" ON public.env_daily_reports
    FOR SELECT USING (device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid()));
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.env_daily_reports;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
