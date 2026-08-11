-- =====================================================================
-- terra-server schedules (2026-08-10) — Stage H-2 예약 타이머
-- 적용 방법: Supabase 대시보드 > SQL Editor 에서 통째로 실행
--
-- 앱이 예약(daily/weekly) 을 등록 → schedule_runner 가 due 된 예약을
-- commands 로 INSERT → 기존 dispatcher 가 MQTT publish.
-- 즉 예약은 "명령 발행을 예약"하는 것이고, 실행 경로는 기존 파이프라인 재사용.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.schedules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    owner_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,             -- 'mist' | 'fan_toggle' | 'led_on' ...
    payload      JSONB,                     -- action 별 인자 (mist: {"duration_ms": 2000})
    kind         TEXT NOT NULL CHECK (kind IN ('daily', 'weekly')),
    time_of_day  TIME NOT NULL,             -- KST 기준 실행 시각 (08:00)
    days_of_week INT[],                     -- weekly 전용. [1..7] (1=월 .. 7=일). daily 는 NULL
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at  TIMESTAMPTZ NOT NULL,      -- 다음 실행 시각 (UTC 로 환산 저장)
    last_run_at  TIMESTAMPTZ,               -- 마지막 발화 시각
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- weekly 는 요일 최소 1개 필수. daily 는 days_of_week 무시.
    CONSTRAINT schedules_weekly_needs_days CHECK (
        kind <> 'weekly'
        OR (days_of_week IS NOT NULL AND array_length(days_of_week, 1) >= 1)
    )
);

-- runner 의 due 조회 최적화 (enabled 인 것만 인덱스)
CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON public.schedules(next_run_at) WHERE enabled;
CREATE INDEX IF NOT EXISTS idx_schedules_owner
    ON public.schedules(owner_id);
CREATE INDEX IF NOT EXISTS idx_schedules_device
    ON public.schedules(device_id);


-- =====================================================================
-- RLS — 본인 소유 사육장(device) 의 예약만 접근
-- =====================================================================
ALTER TABLE public.schedules ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own schedules all" ON public.schedules
    FOR ALL USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
        AND owner_id = auth.uid()
    )
    WITH CHECK (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
        AND owner_id = auth.uid()
    );


-- =====================================================================
-- Realtime — 앱이 예약 변경을 실시간 수신 (다른 기기에서 등록 시 동기화)
-- =====================================================================
ALTER PUBLICATION supabase_realtime ADD TABLE public.schedules;


-- =====================================================================
-- updated_at 자동 갱신 (initial_schema 의 set_updated_at() 재사용)
-- =====================================================================
CREATE TRIGGER trg_schedules_updated_at
    BEFORE UPDATE ON public.schedules
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
