-- 2026-09-30: cameras 라이브 시청 세션 컬럼 — 15분 상한·5분 쉼·한 기기 시청 (owner 결정 2026-09-29)
--
-- 배경(specs/live-view-limit.md): 라이브를 오래 켜두면 카메라 WebRTC 루프가 고착 → stalled → 앱 재연결 반복
-- → 펌웨어 워치독 재부팅(SW:rtc_loop_stall). 두 기기 동시 시청 때도 카메라가 40초 먹통.
-- 서버가 카메라별 현재 시청 세션을 기억해 15분 상한·5분 쉼·한 기기 제한을 강제한다(backend/live_session.py).
--
-- 앱은 cameras 를 Realtime 구독 중이라, 다른 기기가 가져가거나(live_session_id 변경, live_end_reason='taken_over')
-- 15분이 끝나면(live_session_id=NULL, live_end_reason='time_limit') 보던 기기가 바로 알 수 있다.
-- 기존 stream_mode / stream_until 도 같이 쓴다(stream_until = 시청 시작 + 15분).
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. **코드 배포 전에 적용할 것** — 없으면 라이브 시작 시 claim 조회/UPDATE 가
-- 컬럼 없음으로 실패해 라이브가 안 열린다.
-- ⚠️ petcam-lab 과 같은 Supabase 프로젝트 — cameras 는 terra 전용 테이블, 컬럼 추가만이라 기존 코드 영향 없음.

ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_session_id     TEXT;         -- 현재 시청 WebRTC session_id
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_viewer_id      TEXT;         -- 앱 설치별 ID ('legacy' = 구버전 앱)
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_viewer         TEXT;         -- 표시 이름 (예: "iPhone 15")
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_started_at     TIMESTAMPTZ;  -- 연속 시청 시작 (15분 시계 기준)
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_ended_at       TIMESTAMPTZ;  -- 마지막 세션 종료
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_cooldown_until TIMESTAMPTZ;  -- 이 시각까지 새 시청 거절
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS live_end_reason     TEXT;         -- time_limit | taken_over | closed | failed

COMMENT ON COLUMN public.cameras.live_end_reason IS
    '직전 라이브 세션이 끝난 이유: time_limit(15분) | taken_over(다른 기기가 가져감) | closed(앱이 닫음) | failed(연결 실패)';
