-- 2026-09-15: push_outbox — 앱 푸시 알림 이벤트 전송 대기열
--
-- 배경 (앱 회신 docs/BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md §6):
--   예약/타이머로 실행된 명령의 "실제 기기 ACK 가 확정된 시점" 에 앱측 Edge Function
--   (notification-ingest) 으로 이벤트를 보내야 한다. MQTT 수신 스레드를 HTTP 지연으로
--   막으면 안 되고, 5xx·네트워크 오류 재시도도 필요하다.
--   → commands UPDATE 직후 이 테이블에 INSERT 하고, 별도 워커 스레드가 폴링하며 전송.
--
-- 왜 DB 테이블인가: 브리지가 죽어도 미전송 이벤트가 남는다. 메모리 큐면 사라진다.
--   같은 Supabase 프로젝트라 추가 인프라가 필요 없다.
--
-- 멱등: event_id 가 UNIQUE. 같은 (command, phase) 는 한 번만 쌓이고,
--   재시도는 같은 event_id 로 나가서 수신측이 중복 판정할 수 있다(앱 계약 §요청 규칙).
--
-- ⚠️ 이 테이블은 서버 내부용이다. 앱은 직접 읽지 않는다 → RLS 전면 차단(service_role 만).
--
-- 선행 마이그레이션: 2026-05-26_initial_schema.sql (commands)


CREATE TABLE IF NOT EXISTS public.push_outbox (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 앱 계약의 멱등 키. 예: "command:5ec3b4d1-...:started"
    event_id      TEXT NOT NULL UNIQUE,
    -- 앱 계약의 type. 예: device.action.started | device.action.ended | device.action.failed
    event_type    TEXT NOT NULL,
    -- 수신측에 그대로 POST 할 본문 전체(schema_version/type/occurred_at/user_id/payload).
    body          JSONB NOT NULL,

    status        TEXT NOT NULL DEFAULT 'pending',
    -- 'pending'  : 전송 대기
    -- 'sent'     : 2xx 수신 (성공)
    -- 'failed'   : 4xx — 인증/payload 문제. 무한 재시도 안 함(앱 계약)
    -- 'abandoned': 재시도 상한 초과

    attempts      INT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error    TEXT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at       TIMESTAMPTZ
);

-- 워커의 주 조회 경로: 보낼 게 있나? (status='pending' AND next_retry_at <= now())
CREATE INDEX IF NOT EXISTS idx_push_outbox_due
    ON public.push_outbox (next_retry_at)
    WHERE status = 'pending';

-- 운영 조회(실패 원인 확인)
CREATE INDEX IF NOT EXISTS idx_push_outbox_status
    ON public.push_outbox (status, created_at DESC);

COMMENT ON TABLE public.push_outbox
    IS '앱 푸시 이벤트 전송 대기열(서버 내부). 브리지 워커가 Edge Function 으로 POST';
COMMENT ON COLUMN public.push_outbox.event_id
    IS '멱등 키. 재시도 시 동일 값 사용 → 수신측 중복 판정 가능';
COMMENT ON COLUMN public.push_outbox.body
    IS 'Edge Function 에 POST 할 본문 전체. secret 은 포함하지 않는다(헤더로만)';


-- =====================================================================
-- RLS — 앱 접근 전면 차단
-- =====================================================================
-- 정책을 하나도 만들지 않으면 anon/authenticated 는 아무것도 못 한다.
-- service_role(브리지)만 RLS 를 바이패스해 읽고 쓴다.

ALTER TABLE public.push_outbox ENABLE ROW LEVEL SECURITY;


-- =====================================================================
-- 정리 cron — 전송 완료 7일 후 삭제
-- =====================================================================
-- sent 행이 영원히 쌓이면 곤란하다. 실패(failed/abandoned)는 원인 추적용으로
-- 30일 남긴다.

SELECT cron.schedule(
    'cleanup-push-outbox',
    '17 * * * *',                              -- 매시 17분 (다른 cron 과 시간 분리)
    $$
    DELETE FROM public.push_outbox
     WHERE (status = 'sent'      AND sent_at    < now() - interval '7 days')
        OR (status IN ('failed', 'abandoned') AND created_at < now() - interval '30 days');
    $$
);


-- =====================================================================
-- 적용 확인
-- =====================================================================
--   SELECT status, count(*) FROM public.push_outbox GROUP BY status;
--   SELECT jobname, schedule FROM cron.job ORDER BY jobid;
