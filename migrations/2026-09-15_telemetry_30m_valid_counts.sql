-- 2026-09-15: telemetry_30m 지표별 유효 표본 수 + 센서 fault 값 집계 제외
--
-- 배경 (앱 회신 docs/BACKEND_HANDOFF_REPLY_REDESIGN_2026-09-15.md §5):
--   앱이 과거 일평균을 "가중평균"으로 계산하려면 지표별 유효 표본 수가 필요하다.
--   그런데 기존 sample_count 는 count(*) = 버킷 안의 원본 "행 수" 라서,
--   온도 센서만 죽어 t_a 가 전부 NULL 이어도 값이 그대로다. 가중치로 쓰면 틀린다.
--
--   또 하나: docs/MQTT.md §1 은 "센서 fault 시 ok:false, t/h 값은 무의미" 라고
--   정의하는데, 기존 집계는 a_ok/b_ok 를 보지 않아 무의미한 값이 평균·최소·최대에
--   그대로 섞여 들어갔다. (수집부 쪽 대응은 backend/mqtt/handlers.py 에서 별도로,
--   ok=false 면 t/h 를 NULL 로 저장하도록 함께 수정한다.)
--
-- 이 마이그레이션이 하는 일:
--   1. 지표별 유효 표본 수 컬럼 4개 추가 (t_a_count / h_a_count / t_b_count / h_b_count)
--   2. 집계 cron 재등록 — 모든 집계에 FILTER (WHERE a_ok) / (WHERE b_ok) 적용
--
-- ⚠️ 소급 불가: telemetry 원본은 7일 보존(cleanup-telemetry-7d)이라 그 이전 버킷의
--    지표별 유효 표본 수는 복원할 수 없다. 적용 시점 이후 버킷부터 채워지고, 그 전
--    버킷은 NULL 로 남는다. 앱은 NULL 이면 "미지원"으로 처리한다(회신 §5.5).
--
-- ⚠️ sample_count 는 그대로 둔다(호환). 의미는 계속 "행 수" 다.
--
-- 선행 마이그레이션: 2026-06-30_telemetry_30m_pgcron.sql


-- =====================================================================
-- 1. 지표별 유효 표본 수 컬럼
-- =====================================================================
-- NULL = 이 마이그레이션 적용 전에 만들어진 버킷(복원 불가).

ALTER TABLE public.telemetry_30m
    ADD COLUMN IF NOT EXISTS t_a_count INT,
    ADD COLUMN IF NOT EXISTS h_a_count INT,
    ADD COLUMN IF NOT EXISTS t_b_count INT,
    ADD COLUMN IF NOT EXISTS h_b_count INT;

COMMENT ON COLUMN public.telemetry_30m.sample_count
    IS '버킷 내 원본 telemetry 행 수(빠짐 진단용). 지표별 유효 표본 수가 아님 — t_a_count 등을 쓸 것';
COMMENT ON COLUMN public.telemetry_30m.t_a_count
    IS 'A센서 온도의 유효 표본 수(a_ok=true & t_a NOT NULL). NULL=2026-09-15 이전 버킷(복원 불가)';
COMMENT ON COLUMN public.telemetry_30m.h_a_count
    IS 'A센서 습도의 유효 표본 수(a_ok=true & h_a NOT NULL). NULL=2026-09-15 이전 버킷';
COMMENT ON COLUMN public.telemetry_30m.t_b_count
    IS 'B센서 온도의 유효 표본 수(b_ok=true & t_b NOT NULL). NULL=2026-09-15 이전 버킷';
COMMENT ON COLUMN public.telemetry_30m.h_b_count
    IS 'B센서 습도의 유효 표본 수(b_ok=true & h_b NOT NULL). NULL=2026-09-15 이전 버킷';


-- =====================================================================
-- 2. 집계 cron 재등록 (fault 제외 + 지표별 count)
-- =====================================================================
-- cron.schedule 은 같은 jobname 으로 다시 부르면 기존 잡을 덮어쓴다(upsert).
-- 스케줄('*/30 * * * *')과 90분 재집계 윈도우는 기존과 동일 — 본문만 교체.
--
-- FILTER (WHERE a_ok): 센서가 고장 보고한 샘플을 avg/min/max/count 전부에서 제외.
--   avg/min/max 는 NULL 을 자동 무시하므로, 수집부가 NULL 로 저장하기 시작하면
--   FILTER 는 이중 안전장치가 된다. 적용 직후 7일간은 기존에 쌓인 "ok=false 인데
--   값이 들어있는" 행이 남아 있으므로 FILTER 가 실제로 일을 한다.
--
-- count(t_a) FILTER (...): count 는 NULL 을 세지 않으므로 "유효 표본 수" 가 된다.

SELECT cron.schedule(
    'downsample-telemetry-30m',
    '*/30 * * * *',
    $$
    INSERT INTO public.telemetry_30m (
        device_id, bucket, sample_count,
        t_a_avg, t_a_min, t_a_max, t_a_count,
        h_a_avg, h_a_min, h_a_max, h_a_count,
        t_b_avg, t_b_min, t_b_max, t_b_count,
        h_b_avg, h_b_min, h_b_max, h_b_count
    )
    SELECT
        device_id,
        to_timestamp(floor(extract(epoch FROM ts) / 1800) * 1800) AS bucket,
        count(*),
        avg(t_a) FILTER (WHERE a_ok), min(t_a) FILTER (WHERE a_ok),
        max(t_a) FILTER (WHERE a_ok), count(t_a) FILTER (WHERE a_ok),
        avg(h_a) FILTER (WHERE a_ok), min(h_a) FILTER (WHERE a_ok),
        max(h_a) FILTER (WHERE a_ok), count(h_a) FILTER (WHERE a_ok),
        avg(t_b) FILTER (WHERE b_ok), min(t_b) FILTER (WHERE b_ok),
        max(t_b) FILTER (WHERE b_ok), count(t_b) FILTER (WHERE b_ok),
        avg(h_b) FILTER (WHERE b_ok), min(h_b) FILTER (WHERE b_ok),
        max(h_b) FILTER (WHERE b_ok), count(h_b) FILTER (WHERE b_ok)
    FROM public.telemetry
    WHERE ts >= now() - interval '90 minutes'
    GROUP BY device_id, to_timestamp(floor(extract(epoch FROM ts) / 1800) * 1800)
    ON CONFLICT (device_id, bucket) DO UPDATE SET
        sample_count = EXCLUDED.sample_count,
        t_a_avg = EXCLUDED.t_a_avg, t_a_min = EXCLUDED.t_a_min,
        t_a_max = EXCLUDED.t_a_max, t_a_count = EXCLUDED.t_a_count,
        h_a_avg = EXCLUDED.h_a_avg, h_a_min = EXCLUDED.h_a_min,
        h_a_max = EXCLUDED.h_a_max, h_a_count = EXCLUDED.h_a_count,
        t_b_avg = EXCLUDED.t_b_avg, t_b_min = EXCLUDED.t_b_min,
        t_b_max = EXCLUDED.t_b_max, t_b_count = EXCLUDED.t_b_count,
        h_b_avg = EXCLUDED.h_b_avg, h_b_min = EXCLUDED.h_b_min,
        h_b_max = EXCLUDED.h_b_max, h_b_count = EXCLUDED.h_b_count;
    $$
);


-- =====================================================================
-- 3. 적용 확인
-- =====================================================================
-- 등록된 잡 확인 (본문이 FILTER 포함으로 바뀌었는지):
--   SELECT jobname, schedule, left(command, 200) FROM cron.job
--    WHERE jobname = 'downsample-telemetry-30m';
--
-- 30분 후 새 버킷에 count 가 채워지는지:
--   SELECT bucket, sample_count, t_a_count, h_a_count
--     FROM public.telemetry_30m ORDER BY bucket DESC LIMIT 4;
--   정상이면 sample_count ≈ 600, t_a_count ≈ 600 (센서 정상일 때).
