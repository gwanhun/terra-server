-- =====================================================================
-- terra-server initial schema (2026-05-26)
-- 적용 방법: Supabase 대시보드 > SQL Editor 에서 통째로 실행
--
-- 도메인: 파충류/양서류 사육장 IoT 모니터링/제어
-- 디바이스: ESP32-S3 (DHT22 x2, 워터펌프, 팬, 히터, LED 컨트롤러)
-- =====================================================================

-- 확장 (Supabase 는 pgcrypto 기본 설치되어 있음, gen_random_uuid 제공)
-- CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =====================================================================
-- 1. devices — 사육장(=ESP32 디바이스) 등록
-- =====================================================================
-- 페어링 시 생성. 한 유저가 여러 사육장을 보유 가능.
-- token_hash 는 MQTT 인증용 password 의 bcrypt 해시. 평문은 발급 시점에만 디바이스로 전달.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.devices (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_id     TEXT NOT NULL UNIQUE,        -- ESP32 MQTT client_id (e.g. "terra-a1b2c3")
    token_hash    TEXT NOT NULL,                -- MQTT password bcrypt 해시
    name          TEXT NOT NULL,                -- 사용자가 붙인 이름 ("거실 비어디드")
    species       TEXT,                          -- "bearded_dragon", "leopard_gecko" 등 (NULL 허용)
    firmware_ver  TEXT,                          -- 최근 보고된 펌웨어 버전
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ,                  -- 마지막 telemetry/ack 수신 시각
    is_online     BOOLEAN NOT NULL DEFAULT FALSE -- bridge 가 갱신
);

CREATE INDEX IF NOT EXISTS idx_devices_owner ON public.devices(owner_id);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON public.devices(last_seen_at DESC);


-- =====================================================================
-- 2. device_settings — 사용자별 목표 환경 / 알람 임계값
-- =====================================================================
-- 디바이스당 1:1 행. 종(species)별 기본값은 애플리케이션 레이어에서 프리셋 적용.
-- schedule 은 LED 점등 시간표 등 자유 JSON.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.device_settings (
    device_id           UUID PRIMARY KEY REFERENCES public.devices(id) ON DELETE CASCADE,
    target_temp_min     FLOAT,    -- 목표 온도 하한 (°C)
    target_temp_max     FLOAT,    -- 목표 온도 상한
    target_humid_min    FLOAT,    -- 목표 습도 하한 (%RH)
    target_humid_max    FLOAT,
    alert_temp_high     FLOAT,    -- 이 값 초과 시 알림 (히터 안전 latch 와는 별개)
    alert_temp_low      FLOAT,
    alert_humid_low     FLOAT,
    schedule            JSONB,    -- LED 점등 스케줄 등 (예: {"led_on_hour": 8, "led_off_hour": 20})
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- 3. telemetry — 시계열 원본 (3초 주기, 단기 보관)
-- =====================================================================
-- 원본은 7일만 보관 (pg_cron 으로 자동 삭제). 장기 분석은 telemetry_1m 사용.
-- PRIMARY KEY (device_id, ts) 로 동일 시각 중복 INSERT 방지.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.telemetry (
    device_id     UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    ts            TIMESTAMPTZ NOT NULL,
    t_a           FLOAT,        -- DHT22 #1 온도 (°C)
    h_a           FLOAT,        -- DHT22 #1 습도 (%RH)
    a_ok          BOOLEAN NOT NULL DEFAULT FALSE,
    t_b           FLOAT,        -- DHT22 #2 온도
    h_b           FLOAT,        -- DHT22 #2 습도
    b_ok          BOOLEAN NOT NULL DEFAULT FALSE,
    relay         TEXT,         -- 'ON' | 'OFF' | NULL
    fan           TEXT,
    heater_state  TEXT,
    heater_locked BOOLEAN,
    PRIMARY KEY (device_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON public.telemetry(ts DESC);


-- =====================================================================
-- 4. telemetry_1m — 분 단위 다운샘플 (1년 보관)
-- =====================================================================
-- pg_cron 으로 telemetry → telemetry_1m 집계.
-- 1분 평균/min/max 보관 → 원본 대비 1/20 크기.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.telemetry_1m (
    device_id UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    bucket    TIMESTAMPTZ NOT NULL,
    t_a_avg FLOAT, t_a_min FLOAT, t_a_max FLOAT,
    h_a_avg FLOAT, h_a_min FLOAT, h_a_max FLOAT,
    t_b_avg FLOAT, t_b_min FLOAT, t_b_max FLOAT,
    h_b_avg FLOAT, h_b_min FLOAT, h_b_max FLOAT,
    PRIMARY KEY (device_id, bucket)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_1m_bucket ON public.telemetry_1m(bucket DESC);


-- =====================================================================
-- 5. commands — 명령 이력 (앱→디바이스)
-- =====================================================================
-- 앱이 INSERT → bridge 가 Realtime subscribe → MQTT publish → 디바이스 실행 → ack.
-- status 흐름: pending → sent → acked | rejected | expired
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.commands (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    issued_by   UUID REFERENCES auth.users(id),  -- 시스템 발행 명령은 NULL 허용
    action      TEXT NOT NULL,
    -- 'relay_toggle' | 'fan_toggle' | 'heater_toggle' | 'heater_clear'
    -- 'led_on' | 'led_up' | 'led_down' | 'token_rotate'
    payload     JSONB,                            -- action 별 추가 인자 (token_rotate 의 new_token 등)
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ttl_sec     INT NOT NULL DEFAULT 10,
    status      TEXT NOT NULL DEFAULT 'pending',
    -- 'pending' | 'sent' | 'acked' | 'rejected' | 'expired'
    result      TEXT,                             -- ack 응답 ('ok', 'rejected_locked', ...)
    acked_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_commands_device ON public.commands(device_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_commands_pending ON public.commands(status, issued_at)
    WHERE status = 'pending';


-- =====================================================================
-- 6. alerts — 알림 (과열, 오프라인, latch 등)
-- =====================================================================
-- bridge / 디바이스가 INSERT. 앱은 Realtime 으로 수신.
-- resolved_at NULL 인 행이 "활성 알림".
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.alerts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    -- 'temp_high' | 'temp_low' | 'humid_low' | 'heater_latched'
    -- 'sensor_fault' | 'offline' | 'token_invalid'
    severity      TEXT NOT NULL DEFAULT 'warning',
    -- 'info' | 'warning' | 'critical'
    message       TEXT,
    context       JSONB,    -- 발생 시점 센서값 등
    triggered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alerts_device ON public.alerts(device_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON public.alerts(device_id, kind)
    WHERE resolved_at IS NULL;


-- =====================================================================
-- RLS (Row Level Security) 정책
-- =====================================================================
-- service_role 키는 RLS 바이패스 → bridge / API 서버는 자유롭게 접근.
-- anon 키 + JWT 로 접근하는 앱은 본인 디바이스 행만 보임.
-- =====================================================================

ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY;

-- 본인 디바이스 조회/수정/삭제만 가능
CREATE POLICY "own devices select" ON public.devices
    FOR SELECT USING (auth.uid() = owner_id);
CREATE POLICY "own devices update" ON public.devices
    FOR UPDATE USING (auth.uid() = owner_id);
CREATE POLICY "own devices delete" ON public.devices
    FOR DELETE USING (auth.uid() = owner_id);
-- INSERT 는 RLS 정책 없음 → API (service_role) 만 가능 (페어링 흐름 강제)


ALTER TABLE public.device_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own settings all" ON public.device_settings
    FOR ALL USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    )
    WITH CHECK (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );


ALTER TABLE public.telemetry ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own telemetry select" ON public.telemetry
    FOR SELECT USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );
-- INSERT/UPDATE/DELETE 는 service_role (bridge) 만


ALTER TABLE public.telemetry_1m ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own telemetry_1m select" ON public.telemetry_1m
    FOR SELECT USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );


ALTER TABLE public.commands ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own commands select" ON public.commands
    FOR SELECT USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );
CREATE POLICY "own commands insert" ON public.commands
    FOR INSERT WITH CHECK (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
        AND issued_by = auth.uid()
    );
-- UPDATE 는 bridge 만 (status 갱신)


ALTER TABLE public.alerts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own alerts select" ON public.alerts
    FOR SELECT USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );
CREATE POLICY "own alerts resolve" ON public.alerts
    FOR UPDATE USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );
-- INSERT 는 bridge 만 (사용자가 직접 알림 발행 금지)


-- =====================================================================
-- Realtime publication — 앱이 변경을 실시간으로 받기 위함
-- =====================================================================
-- Supabase 는 supabase_realtime publication 으로 묶인 테이블 변경을
-- WebSocket 으로 push. 앱이 telemetry, commands, alerts 변경 구독.
-- =====================================================================

ALTER PUBLICATION supabase_realtime ADD TABLE public.telemetry;
ALTER PUBLICATION supabase_realtime ADD TABLE public.commands;
ALTER PUBLICATION supabase_realtime ADD TABLE public.alerts;
ALTER PUBLICATION supabase_realtime ADD TABLE public.devices;


-- =====================================================================
-- 트리거: updated_at 자동 갱신
-- =====================================================================

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_devices_updated_at
    BEFORE UPDATE ON public.devices
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_device_settings_updated_at
    BEFORE UPDATE ON public.device_settings
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
