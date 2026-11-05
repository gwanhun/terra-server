-- 2026-11-05: 자동 환기·자동 분무 (specs/stage-r-auto-env.md, 앱 요청 2026-11-05 C1~C3)
ALTER TABLE public.device_settings
    ADD COLUMN IF NOT EXISTS auto_vent_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS auto_mist_enabled BOOLEAN NOT NULL DEFAULT false;

-- 브리지가 쓰는 현재 상태(R12). 앱은 읽기만 한다(기존 devices RLS·Realtime 그대로 — 소유자 쓰기는 막지 않고 브리지가 덮어씀).
ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS auto_env_state JSONB;

ALTER TABLE public.commands DROP CONSTRAINT IF EXISTS commands_source_check;
ALTER TABLE public.commands ADD CONSTRAINT commands_source_check
    CHECK (source IN ('manual', 'schedule', 'timer', 'guard', 'restore', 'auto'));
