-- =====================================================================
-- terra-server — commands 출처 구분 + schedules 스마트 조건(guard) (2026-08-12)
-- 적용: Supabase 대시보드 > SQL Editor 에서 통째로 실행
--
-- 배경 (앱 개발자 요청 2·5):
--  - 요청 5: 감사 로그가 "누가·왜 냈나(수동/예약/타이머/가드)" 를 요구 → commands 에 출처 컬럼
--  - 요청 2: 예약에 스마트 조건(가드). 이번엔 skip 형(발행 직전 판단)만 서버가 평가.
--    stop 형(가동 중 정지)은 펌웨어 담당(후속).
-- =====================================================================

-- ---------- commands: 출처 구분 ----------
ALTER TABLE public.commands
    ADD COLUMN IF NOT EXISTS source    TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS source_id UUID,   -- schedules.id 등 (nullable)
    ADD COLUMN IF NOT EXISTS reason    TEXT;    -- 가드 사유 등 (nullable)

-- source 값 제약 (기존 행은 DEFAULT 'manual' 로 채워짐)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'commands_source_check'
    ) THEN
        ALTER TABLE public.commands
            ADD CONSTRAINT commands_source_check
            CHECK (source IN ('manual', 'schedule', 'timer', 'guard'));
    END IF;
END $$;

-- 감사 로그 필터(출처별) 조회 최적화
CREATE INDEX IF NOT EXISTS idx_commands_source
    ON public.commands(device_id, source, issued_at DESC);


-- ---------- schedules: 스마트 조건(guard) ----------
-- guard 예: {"type":"skip_when_humidity_above","value":70.0,"enabled":true}
--  - skip_when_humidity_above / skip_when_humidity_below
--  - skip_when_temp_above / skip_when_temp_below
--  (stop_when_* 는 펌웨어에서 처리 — 서버는 무시하고 정상 발행)
ALTER TABLE public.schedules
    ADD COLUMN IF NOT EXISTS guard JSONB;
