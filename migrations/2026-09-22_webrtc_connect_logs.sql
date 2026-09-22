-- 2026-09-22: webrtc_connect_logs — 라이브 연결 시도 1건당 1행 (측정용)
--
-- 출처: 앱팀 핸드오프 2026-09-22-camera-live-stability-server-requests.md 요청 1.
-- 목적: 라이브 실패가 앱·펌웨어·NAT 중 어디서 생기는지 비율을 알기 위함.
--       지금은 실패가 디버그 로그에만 남아 집계가 불가능하다.
--
-- 수집 경로: 앱이 Supabase 로 직접 INSERT (terra-server REST 경유 안 함).
--   왜 직접인가 — 연결이 실패하는 순간은 네트워크가 나쁜 순간이다. 우리 API 를 한 단계
--   더 태우면 정작 기록해야 할 실패 케이스에서 로그 자체가 유실된다. Supabase 직행이 튼튼.
--
-- 조회: service_role 만 (RLS 에 SELECT 정책을 두지 않음 = 앱은 자기 행도 못 읽는다).
--       분석은 Supabase SQL 편집기나 백엔드에서.
--
-- ⚠️ 앱팀 제안 DDL 에서 바꾼 것 (아래 각 위치에 근거 주석):
--   1) user_id FK 에 ON DELETE CASCADE 추가
--   2) local_cand / remote_cand 에 CHECK 제약 추가 (outcome 은 일부러 안 걸었다 — 본문 참고)
--   3) (created_at DESC) 인덱스 추가
--   4) 90일 보존 cron 추가 (제안서에 "90일이면 충분"만 있고 수단이 없었음)
--   5) offer_attempts / answer_ms 컬럼 추가 — 서버만 아는 값 (§마지막 주석 참고)

-- =====================================================================
-- 1. 테이블
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.webrtc_connect_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- ON DELETE CASCADE 는 이 레포 전 테이블의 owner FK 컨벤션(initial_schema/camera_schema).
    -- 빠뜨리면 계정 삭제가 FK 로 막힌다. auth.users 하드 삭제는 이미 트리거 문제로
    -- 전역 실패하는 상태라(운영 노트), FK 를 하나라도 더 걸어두면 안 된다.
    user_id     UUID NOT NULL DEFAULT auth.uid()
                REFERENCES auth.users(id) ON DELETE CASCADE,
    camera_id   UUID NOT NULL REFERENCES public.cameras(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    app_version TEXT,
    platform    TEXT,
    network     TEXT,

    -- outcome 에는 일부러 CHECK 를 걸지 않는다.
    -- 이 테이블의 목적은 측정이고, CHECK 가 거부하면 정작 측정하려던 실패 케이스의 행이
    -- 통째로 날아간다(앱은 23514 예외를 가장 네트워크가 나쁜 순간에 받는다). 더러운 값은
    -- GROUP BY 로 바로 드러나고 나중에 정리할 수 있지만, 없는 행은 복구가 안 된다.
    -- 앱이 정의하는 어휘라 나중에 늘어날 수도 있다(no_answer/timeout 등).
    -- 값이 굳으면 그때 CHECK 를 추가한다:
    --   ALTER TABLE public.webrtc_connect_logs ADD CONSTRAINT webrtc_connect_logs_outcome_chk
    --       CHECK (outcome IN (...)) NOT VALID;   -- NOT VALID = 기존 행 재검증 생략
    -- 앱 계약상 값: streaming / failed / unresponsive / no_video / stalled / cancelled
    outcome     TEXT NOT NULL,
    fail_phase  TEXT,

    ms_config       INT,
    ms_answer       INT,
    ms_connected    INT,
    ms_first_frame  INT,

    -- 선택된 ICE 후보 타입. relay 가 찍히는지가 TURN 도입 성공 판정 기준.
    -- 여기는 CHECK 를 건다 — W3C RTCIceCandidateType 의 전체 집합이라 늘어날 일이 없다.
    local_cand  TEXT CHECK (local_cand  IN ('host','srflx','prflx','relay')),
    remote_cand TEXT CHECK (remote_cand IN ('host','srflx','prflx','relay')),

    reconnect_attempt INT,
    streamed_sec      INT,

    -- 서버만 아는 값. 앱은 POST /webrtc/offer 응답에서 받아 그대로 넣는다.
    -- 이게 있어야 "펌웨어가 answer 를 못 만든 것"과 "NAT 가 막은 것"이 갈린다
    -- (backend/routers/webrtc.py 의 attempts=3 × per_timeout=7s 재시도).
    offer_attempts INT,
    answer_ms      INT
);

COMMENT ON TABLE public.webrtc_connect_logs IS
    '라이브(WebRTC) 연결 시도 1건당 1행. 앱이 직접 INSERT, 조회는 service_role. 90일 보존.';
COMMENT ON COLUMN public.webrtc_connect_logs.offer_attempts IS
    '서버가 카메라에 offer 를 몇 번 재발행했는지(1=첫 시도 성공). POST /webrtc/offer 응답값.';
COMMENT ON COLUMN public.webrtc_connect_logs.answer_ms IS
    '서버가 offer 를 처음 발행한 뒤 카메라 answer 를 받기까지 ms. POST /webrtc/offer 응답값.';
COMMENT ON COLUMN public.webrtc_connect_logs.local_cand IS
    '앱 측 선택 후보 타입. relay = TURN 경유. TURN 도입 완료 판정에 사용.';

-- =====================================================================
-- 2. RLS — 본인 행 INSERT 만. SELECT 정책 없음 = service_role 전용 조회.
-- =====================================================================

ALTER TABLE public.webrtc_connect_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "insert own" ON public.webrtc_connect_logs;
CREATE POLICY "insert own" ON public.webrtc_connect_logs
    FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid());

-- =====================================================================
-- 3. 인덱스
-- =====================================================================

-- 카메라 1대의 이력 (개별 기기 디버깅)
CREATE INDEX IF NOT EXISTS webrtc_connect_logs_camera_time
    ON public.webrtc_connect_logs (camera_id, created_at DESC);

-- 전역 집계 — "최근 24시간 전체 실패율/후보 타입 분포"가 이 테이블의 주 용도인데
-- camera_id 선두 인덱스로는 못 탄다. 보존 cron 의 범위 DELETE 도 이 인덱스를 쓴다.
CREATE INDEX IF NOT EXISTS webrtc_connect_logs_time
    ON public.webrtc_connect_logs (created_at DESC);

-- =====================================================================
-- 4. 보존 cron — 매일 90일 이전 DELETE
-- =====================================================================
-- 패턴 출처: 2026-06-30_telemetry_30m_pgcron.sql §4 (cleanup-telemetry-7d).
-- cron.schedule 은 같은 jobname 으로 다시 부르면 덮어쓴다(upsert).

SELECT cron.schedule(
    'cleanup-webrtc-connect-logs-90d',
    '23 4 * * *',                              -- 매일 04:23 UTC (기존 잡들과 시간 회피)
    $$
    DELETE FROM public.webrtc_connect_logs
    WHERE created_at < now() - interval '90 days';
    $$
);

-- =====================================================================
-- 검증 / 분석 쿼리 (적용 후 수동 실행 — 참고용)
-- =====================================================================
-- 등록 확인:
--   SELECT jobid, schedule, jobname, active FROM cron.job
--   WHERE jobname = 'cleanup-webrtc-connect-logs-90d';
--
-- 최근 24시간 결과 분포 (요청 1의 본래 목적):
--   SELECT outcome, count(*), round(100.0*count(*)/sum(count(*)) OVER (), 1) AS pct
--   FROM public.webrtc_connect_logs
--   WHERE created_at > now() - interval '24 hours'
--   GROUP BY outcome ORDER BY 2 DESC;
--
-- 실패가 어디서 나는가 — 서버 재시도 횟수와 교차:
--   SELECT offer_attempts, outcome, count(*)
--   FROM public.webrtc_connect_logs
--   WHERE created_at > now() - interval '7 days'
--   GROUP BY 1, 2 ORDER BY 1, 3 DESC;
--   -- offer_attempts>1 이 많으면 펌웨어(esp_peer_open PSRAM) 문제,
--   -- offer_attempts=1 인데 outcome='failed' 면 NAT/ICE 문제로 갈린다.
--
-- TURN 도입 효과 (relay 비율):
--   SELECT network, local_cand, count(*)
--   FROM public.webrtc_connect_logs
--   WHERE created_at > now() - interval '24 hours'
--   GROUP BY 1, 2 ORDER BY 1, 3 DESC;
