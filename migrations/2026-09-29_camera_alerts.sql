-- 2026-09-29: camera_alerts — 카메라 부분 멈춤 알림 (비정상 재시작 · 업로드 정체)
--
-- 출처: specs/camera-reliability-2026-09.md P3. 베타 카메라가 heartbeat 는 살아있는데 녹화·업로드만
-- 멈추고 사람이 재부팅해야 회복됐다. 서버는 warning 로그로만 남겨 아무도 몰랐다.
--
-- 왜 alerts 를 안 쓰나: alerts.device_id 는 devices FK NOT NULL 이라 카메라를 담을 수 없고, 앱이
-- alerts 를 Realtime 구독 중이라 스키마를 바꾸면 앱 계약이 흔들린다. 같은 모양의 카메라 전용 테이블.
--
-- 쓰기: 브리지(service_role)만. backend/camera_alerts.py
--   kind = 'camera_abnormal_reset'  새 부팅의 reset 이 PANIC/WDT/INT_WDT/TASK_WDT/SW:rtc_loop_stall/SW:upload_stuck
--          'camera_upload_stalled'  업로드 성공이 30분+ 멈춘 채 실패만 3회+ 증가
--   resolved_at = 업로드가 다시 성공한 시각. NULL = 활성.
-- 조회: 본인 카메라 SELECT(앱이 나중에 쓸 수 있게 alerts 와 같은 방식) + service_role.
--   owner 결정 09-29: 알림 행만. 사용자 푸시·자동 재부팅 없음(푸시는 앱 계약 후 별도). Realtime 미등록.
-- ⚠️ petcam-lab 과 같은 Supabase 프로젝트 — 새 테이블이라 기존 테이블·트리거 영향 없음.

CREATE TABLE IF NOT EXISTS public.camera_alerts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id     UUID NOT NULL REFERENCES public.cameras(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    -- 'camera_abnormal_reset' | 'camera_upload_stalled'
    severity      TEXT NOT NULL DEFAULT 'warning',
    message       TEXT,
    context       JSONB,    -- 발생 시점 heartbeat 값 (reset, up_ok/up_fail/sd_*, last_err …)
    triggered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ
);

COMMENT ON TABLE public.camera_alerts IS
    '카메라 부분 멈춤 알림(비정상 재시작·업로드 정체). 브리지 INSERT, resolved_at=업로드 회복 시각.';

CREATE INDEX IF NOT EXISTS idx_camera_alerts_camera
    ON public.camera_alerts (camera_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_camera_alerts_active
    ON public.camera_alerts (camera_id, kind) WHERE resolved_at IS NULL;

ALTER TABLE public.camera_alerts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own camera alerts select" ON public.camera_alerts;
CREATE POLICY "own camera alerts select" ON public.camera_alerts
    FOR SELECT USING (
        camera_id IN (SELECT id FROM public.cameras WHERE owner_id = auth.uid())
    );
-- INSERT/UPDATE 는 bridge(service_role)만.

-- 검증 / 분석 (적용 후 수동 실행 — 참고용)
--   활성 알림:   SELECT c.camera_id, a.kind, a.triggered_at, a.context
--                FROM public.camera_alerts a JOIN public.cameras c ON c.id = a.camera_id
--                WHERE a.resolved_at IS NULL ORDER BY a.triggered_at DESC;
--   7일 크래시:  SELECT context->>'reset' AS reset, count(*) FROM public.camera_alerts
--                WHERE kind = 'camera_abnormal_reset' AND triggered_at > now() - interval '7 days'
--                GROUP BY 1 ORDER BY 2 DESC;
