-- 2026-09-17: cameras.image_state — 카메라 노출/야간 상태 (펌웨어 텔레메트리 "img")
-- 야간 AE 헌팅(노출이 밝았다 어두웠다 흔들림) 진단용. ae_frozen = 펌웨어가 진동을 감지해 AE 를 동결.
-- 값 예) {"exp":120,"luma":80,"chroma":5,"night":true,"ae_auto":true,"ae_frozen":false}
--
-- ⚠️ 코드(handlers.py)가 9/17 부터 이 컬럼에 썼지만 파일이 없어 적용이 누락됐다 → heartbeat UPDATE 가
-- 통째로 실패해 녹화 중인 카메라가 전부 오프라인으로 보였다. 2026-09-28 운영 적용.
ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS image_state JSONB;
COMMENT ON COLUMN public.cameras.image_state IS '펌웨어 노출/야간 상태(15초 heartbeat img): exp, luma, chroma, night, ae_auto, ae_frozen. null = 구 펌웨어';
