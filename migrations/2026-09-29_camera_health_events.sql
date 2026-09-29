-- 2026-09-29: camera_health_events — 카메라 진단값 이력 (재시작 이벤트 + 10분 스냅샷)
--
-- 출처: specs/camera-reliability-2026-09.md P1 (베타 카메라 "heartbeat 는 살아있는데 녹화·라이브만 멈춤").
-- 문제: heartbeat 의 clip_stats(업로드 성공/실패·SD 적체·last_err)와 sys(uptime·reset)가
--       cameras 행에 최신값으로 덮어써져, PANIC·워치독 재시작과 업로드 실패 추세가 전부 유실됐다.
--
-- 쓰기: 브리지(service_role)만. backend/camera_health.py
--   - kind='reset'    새 부팅 감지 시 즉시 1행 (부팅 시각 = at - uptime_s 비교)
--   - kind='snapshot' 카메라당 10분마다 1행 (15초 heartbeat 전부 저장 금지 — Disk IO 예산)
--   예상량: 활성 카메라 23대 × 144/일 ≈ 3,300행/일, 30일 ≈ 10만 행.
-- 조회: service_role 만 (SELECT 정책 없음). 앱 노출이 필요해지면 그때 owner 정책 추가.
-- ⚠️ petcam-lab 과 같은 Supabase 프로젝트 — 새 테이블이라 기존 테이블·트리거 영향 없음.

-- =====================================================================
-- 1. 테이블
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.camera_health_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    camera_id   UUID NOT NULL REFERENCES public.cameras(id) ON DELETE CASCADE,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL CHECK (kind IN ('reset', 'snapshot')),
    -- sys (펌웨어 heartbeat uptime_sec / reset / free_heap / wifi_rssi)
    uptime_s    INT,
    reset       TEXT,
    heap        INT,
    rssi        INT,
    fw          TEXT,
    -- clips (clip_stats 중 추세를 볼 핵심값). last_err 는 구 펌웨어(fb2-p4 0.1.0)엔 없다.
    up_ok       INT,
    up_fail     INT,
    sd_backlog  INT,
    last_rec_s  INT,
    last_err    JSONB,
    -- clips 원본 전체(rec/skip/skip_lock/sd_ok/sd_fail/up_busy_s …) — 새 카운터가 생겨도 유실 없게.
    stats       JSONB
);

COMMENT ON TABLE public.camera_health_events IS
    '카메라 진단값 이력. 브리지가 재시작(reset) 즉시 + 10분 스냅샷으로 INSERT. service_role 조회. 30일 보존.';
COMMENT ON COLUMN public.camera_health_events.reset IS
    '펌웨어가 보고한 마지막 리셋 사유. PANIC/INT_WDT/TASK_WDT=크래시, BROWNOUT=전원, SW:<why>=펌웨어 자체 재시작.';

-- =====================================================================
-- 2. RLS — 정책 없음 = service_role 전용 (webrtc_connect_logs 와 같은 방식)
-- =====================================================================

ALTER TABLE public.camera_health_events ENABLE ROW LEVEL SECURITY;

-- =====================================================================
-- 3. 인덱스
-- =====================================================================

-- 카메라 1대 이력 + 브리지 재시작 시 마지막 행 조회(backend/camera_health.py _boot_from_history)
CREATE INDEX IF NOT EXISTS camera_health_events_camera_at
    ON public.camera_health_events (camera_id, at DESC);

-- 전역 집계("최근 24시간 재시작 사유 분포") + 보존 cron 범위 DELETE
CREATE INDEX IF NOT EXISTS camera_health_events_at
    ON public.camera_health_events (at DESC);

-- =====================================================================
-- 4. 보존 cron — 매일 30일 이전 DELETE (owner 결정 2026-09-29)
-- =====================================================================
-- 패턴 출처: 2026-09-22_webrtc_connect_logs.sql §4. 같은 jobname 재호출은 덮어쓴다(upsert).

SELECT cron.schedule(
    'cleanup-camera-health-events-30d',
    '37 4 * * *',                              -- 매일 04:37 UTC (기존 04:23 잡과 시간 회피)
    $$
    DELETE FROM public.camera_health_events
    WHERE at < now() - interval '30 days';
    $$
);

-- =====================================================================
-- 검증 / 분석 쿼리 (적용 후 수동 실행 — 참고용)
-- =====================================================================
-- 등록 확인:
--   SELECT jobid, schedule, jobname, active FROM cron.job
--   WHERE jobname = 'cleanup-camera-health-events-30d';
--
-- 최근 7일 재시작 사유 분포:
--   SELECT reset, count(*) FROM public.camera_health_events
--   WHERE kind = 'reset' AND at > now() - interval '7 days'
--   GROUP BY 1 ORDER BY 2 DESC;
--
-- 카메라 1대 업로드 실패 추세:
--   SELECT at, kind, uptime_s, reset, up_ok, up_fail, sd_backlog, last_rec_s, last_err
--   FROM public.camera_health_events
--   WHERE camera_id = '<uuid>' AND at > now() - interval '24 hours'
--   ORDER BY at;
