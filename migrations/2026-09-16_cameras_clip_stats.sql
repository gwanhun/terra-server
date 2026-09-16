-- 2026-09-16: cameras.clip_stats — 카메라 클립 파이프라인 카운터 (펌웨어 텔레메트리 "clips")
-- 크랑이 캠이 27시간 동안 heartbeat 만 정상이고 녹화 0건이던 "조용한 정지"를 서버에서 보기 위함.
-- 값 예) {"rec":12,"skip":0,"skip_lock":0,"up_ok":11,"up_fail":1,"sd_ok":1,"sd_fail":0,
--         "sd_backlog":0,"last_rec_s":340,"up_busy_s":-1}   (부팅 후 누적, -1 = 없음)
ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS clip_stats JSONB,
    ADD COLUMN IF NOT EXISTS clip_stats_at TIMESTAMPTZ;
COMMENT ON COLUMN public.cameras.clip_stats IS '펌웨어 클립 파이프라인 카운터(텔레메트리 clips). NULL=미보고(구 펌웨어)';
COMMENT ON COLUMN public.cameras.clip_stats_at IS 'clip_stats 마지막 갱신 시각';
