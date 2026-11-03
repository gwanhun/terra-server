-- 2026-11-03: 습기 사이클 (기획 v4 0-2·1-2·1-3·1-6, specs/stage-o-humid-cycle-v4.md Task 3)
-- 습기 이벤트 1개 = 1행. 브리지(service_role)만 쓰고, 앱은 RLS 로 본인 기기 행만 SELECT.
-- 앱 칩은 alerts(env_vent_delay·env_mist_gap)로 그리므로 Realtime publication 은 넣지 않는다.
CREATE TABLE IF NOT EXISTS public.humid_cycles (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id            UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    started_at           TIMESTAMPTZ NOT NULL,           -- 급상승 시작(3분 최저값 시각)
    start_humid          NUMERIC(5,1) NOT NULL,
    detected_at          TIMESTAMPTZ NOT NULL,           -- 마지막 (병합) 상승 감지 시각
    max_until            TIMESTAMPTZ NOT NULL,           -- MAX 측정 창 끝 = detected_at + 3분
    max_raw              NUMERIC(5,1) NOT NULL,          -- 지금까지 관측한 최고값(상한 전)
    max_humid            NUMERIC(5,1),                   -- MAX(상한 85). NULL = 측정 중
    dry_target           NUMERIC(5,1),                   -- max_humid − 12. NULL = 확정 전
    dry_reached_at       TIMESTAMPTZ,                    -- 최초 도달(이후 바뀌지 않음)
    status               TEXT NOT NULL
                         CHECK (status IN ('open', 'reached_in_time', 'reached_late', 'not_reached')),
    closed_at            TIMESTAMPTZ,                    -- not_reached/해제/스위치 끔으로 닫힌 시각
    ended_by             TEXT CHECK (ended_by IN ('next_event', 'unlinked', 'disabled')),
    offline_sec          INTEGER NOT NULL,               -- 시작 후 센서 공백 누적(6·15시간 판정에서 뺌)
    merged_count         SMALLINT NOT NULL,              -- 20분 안 재급상승 병합 횟수
    vent_delay_alert_id  UUID REFERENCES public.alerts(id) ON DELETE SET NULL,
    mist_gap_alert_id    UUID REFERENCES public.alerts(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL,
    UNIQUE (device_id, started_at)
);
CREATE INDEX IF NOT EXISTS idx_humid_cycles_device ON public.humid_cycles(device_id, started_at DESC);
ALTER TABLE public.humid_cycles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own humid_cycles select" ON public.humid_cycles;
CREATE POLICY "own humid_cycles select" ON public.humid_cycles
    FOR SELECT USING (device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid()));
