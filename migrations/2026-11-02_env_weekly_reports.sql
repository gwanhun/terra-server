-- 2026-11-02: 주간 온습도 분석 카드 + 예외 안내 (기획 v4 3-1~3-3, 4-1, specs/stage-q-weekly-analysis-v4.md Task 2)
--
-- 브리지(analysis_runner, service_role)가 가장 최근에 끝난 주(월 08:00 ~ 다음 월 08:00 KST)를 기기별로 upsert.
-- 앱은 RLS 로 본인 기기 행만 SELECT + Realtime 구독. INSERT/UPDATE 정책이 없어 authenticated 는 쓰기 거부.
-- 재페어링은 새 devices 행이라 이전 소유자 리포트가 보이지 않는다(env_daily_reports 와 같음).

CREATE TABLE IF NOT EXISTS public.env_weekly_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,                 -- 집계 시작 월요일(KST): week_start 08:00 ~ +7일 08:00
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    temp_card     JSONB,                         -- Card.to_json() | NULL
    humid_card    JSONB,
    notices       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{code,metric,title,body}]
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, week_start)
);
CREATE INDEX IF NOT EXISTS idx_env_weekly_reports_device ON public.env_weekly_reports(device_id, week_start DESC);
ALTER TABLE public.env_weekly_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own env_weekly_reports select" ON public.env_weekly_reports;
CREATE POLICY "own env_weekly_reports select" ON public.env_weekly_reports
    FOR SELECT USING (device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid()));
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.env_weekly_reports;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
