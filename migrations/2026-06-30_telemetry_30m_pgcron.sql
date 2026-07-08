-- =====================================================================
-- terra-server — 온습도 30분 다운샘플 + 원본 정리 (2026-06-30)
-- 적용 방법: Supabase 대시보드 > SQL Editor 에서 통째로 실행
--
-- 목적:
--   1) telemetry(3초 원본) → 30분 단위 AVG/MIN/MAX 집계 → telemetry_30m (영구 보관)
--   2) telemetry 원본은 7일만 보관 (무한 증식 방지)
--
-- 왜 pg_cron?
--   - DB 안에서 완결 → 외부(브리지) 스케줄러 불필요, 브리지가 죽어도 동작
--   - Supabase 공식 지원, 트랜잭션 보장
--   기존 telemetry_1m 은 건드리지 않음 (빈 채 존속, 분 단위 필요 시 차후 활성화).
-- =====================================================================


-- =====================================================================
-- 1. telemetry_30m — 30분 단위 다운샘플 (영구 보관)
-- =====================================================================
-- telemetry_1m 과 동일 구조. bucket 만 30분 경계.
-- 30분 1행 = 디바이스당 하루 48행, 1년 ~1.7만 행 → 사실상 영구 보관 가능.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.telemetry_30m (
    device_id UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    bucket    TIMESTAMPTZ NOT NULL,           -- 30분 경계 (예: 12:00, 12:30)
    sample_count INT,                          -- 이 버킷에 들어간 원본 행 수 (빠짐 진단용)
    t_a_avg FLOAT, t_a_min FLOAT, t_a_max FLOAT,
    h_a_avg FLOAT, h_a_min FLOAT, h_a_max FLOAT,
    t_b_avg FLOAT, t_b_min FLOAT, t_b_max FLOAT,
    h_b_avg FLOAT, h_b_min FLOAT, h_b_max FLOAT,
    PRIMARY KEY (device_id, bucket)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_30m_bucket ON public.telemetry_30m(bucket DESC);


-- RLS: 본인 디바이스만 조회. INSERT/UPDATE 는 pg_cron(=DB 내부, service_role 컨텍스트)만.
ALTER TABLE public.telemetry_30m ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own telemetry_30m select" ON public.telemetry_30m
    FOR SELECT USING (
        device_id IN (SELECT id FROM public.devices WHERE owner_id = auth.uid())
    );


-- =====================================================================
-- 2. pg_cron 확장 활성화
-- =====================================================================
-- Supabase: Dashboard > Database > Extensions 에서 pg_cron 켜도 되고, 아래 SQL 로도 됨.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pg_cron;


-- =====================================================================
-- 3. 집계 cron — 매 30분: telemetry → telemetry_30m UPSERT
-- =====================================================================
-- 최근 90분(=3개 버킷)을 매번 재집계 → UPSERT.
--   왜 90분? 매 30분 실행 시 직전 버킷 + 경계에 늦게 도착한 데이터까지 보정.
--   진행 중인 현재 버킷도 부분 집계되지만, 다음 실행에서 UPSERT 로 갱신되어 결국 정확.
--
-- 30분 버킷 계산: epoch 초를 1800(=30분)으로 내림 → to_timestamp.
--   date_trunc 은 30분 단위를 직접 지원하지 않아 epoch 방식 사용.
-- =====================================================================

SELECT cron.schedule(
    'downsample-telemetry-30m',
    '*/30 * * * *',                            -- 매시 00분, 30분
    $$
    INSERT INTO public.telemetry_30m (
        device_id, bucket, sample_count,
        t_a_avg, t_a_min, t_a_max,
        h_a_avg, h_a_min, h_a_max,
        t_b_avg, t_b_min, t_b_max,
        h_b_avg, h_b_min, h_b_max
    )
    SELECT
        device_id,
        to_timestamp(floor(extract(epoch FROM ts) / 1800) * 1800) AS bucket,
        count(*),
        avg(t_a), min(t_a), max(t_a),
        avg(h_a), min(h_a), max(h_a),
        avg(t_b), min(t_b), max(t_b),
        avg(h_b), min(h_b), max(h_b)
    FROM public.telemetry
    WHERE ts >= now() - interval '90 minutes'
    GROUP BY device_id, to_timestamp(floor(extract(epoch FROM ts) / 1800) * 1800)
    ON CONFLICT (device_id, bucket) DO UPDATE SET
        sample_count = EXCLUDED.sample_count,
        t_a_avg = EXCLUDED.t_a_avg, t_a_min = EXCLUDED.t_a_min, t_a_max = EXCLUDED.t_a_max,
        h_a_avg = EXCLUDED.h_a_avg, h_a_min = EXCLUDED.h_a_min, h_a_max = EXCLUDED.h_a_max,
        t_b_avg = EXCLUDED.t_b_avg, t_b_min = EXCLUDED.t_b_min, t_b_max = EXCLUDED.t_b_max,
        h_b_avg = EXCLUDED.h_b_avg, h_b_min = EXCLUDED.h_b_min, h_b_max = EXCLUDED.h_b_max;
    $$
);


-- =====================================================================
-- 4. 정리 cron — 매시간: telemetry 원본 7일 이전 DELETE
-- =====================================================================
-- 30분 집계가 telemetry_30m 에 영구 보존되므로 원본은 7일이면 충분.
-- 안 켜면 3초 원본이 영원히 쌓임 (디바이스 1대 ≈ 7일 20만 행 / 30MB).
-- =====================================================================

SELECT cron.schedule(
    'cleanup-telemetry-7d',
    '7 * * * *',                               -- 매시 07분 (집계 cron 과 시간 겹침 회피)
    $$
    DELETE FROM public.telemetry
    WHERE ts < now() - interval '7 days';
    $$
);


-- =====================================================================
-- 검증 쿼리 (적용 후 수동 실행 — 참고용, cron 과 무관)
-- =====================================================================
-- 등록된 cron job 확인:
--   SELECT jobid, schedule, jobname, active FROM cron.job;
--
-- 최근 cron 실행 결과 (성공/실패):
--   SELECT jobid, status, return_message, start_time
--   FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
--
-- 30분 집계가 채워지는지 (최근 24시간):
--   SELECT device_id, bucket, sample_count, t_a_avg, t_a_min, t_a_max
--   FROM public.telemetry_30m
--   WHERE bucket >= now() - interval '24 hours'
--   ORDER BY device_id, bucket DESC;
--
-- 버킷 빠짐 진단 (sample_count 가 비정상적으로 낮은 구간 = 데이터 누락):
--   정상 한 버킷 ≈ 30분 / 3초 = 600행. 절반 미만이면 디바이스 오프라인/끊김 의심.
--   SELECT device_id, bucket, sample_count FROM public.telemetry_30m
--   WHERE sample_count < 300 ORDER BY bucket DESC;
--
-- cron job 제거가 필요할 때:
--   SELECT cron.unschedule('downsample-telemetry-30m');
--   SELECT cron.unschedule('cleanup-telemetry-7d');
-- =====================================================================
