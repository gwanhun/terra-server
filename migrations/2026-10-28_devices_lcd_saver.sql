-- 2026-10-28: devices.lcd_* — LCD 전원/소등 시간대/화면보호기/이미지 확정값 (specs/stage-n-lcd-saver-power.md)
--
-- lcd_text(2026-10-07) 와 같은 관례: 서버는 명령만 큐잉하고, 기기가 result=ok 로 ACK 한 시점에
-- 여기 확정한다. 오프라인·만료·거부면 기기 화면이 안 바뀌었으니 그대로. 앱은 devices SELECT·Realtime
-- 으로 그대로 읽는다(소유자 RLS 기존).
--
-- 규칙 자체(소등 시간대·보호기 주기)는 기기 NVS 가 진실이고 여기는 "서버가 마지막으로 확인한 값".
-- 이미지 본체는 R2(lcd/{uuid}/{sha8}.rgb565) + 기기 raw 플래시. lcd_image_key 는 R2 키 접두.
-- 기기가 flash 로 초기화되면 어긋날 수 있다 — lcd_text 와 같은 한계.
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. 코드보다 먼저 적용할 것 — 없으면 ACK 때 lcd_* UPDATE 만
-- 실패하고(로그) ack 처리 자체는 계속된다. GET /devices/{id}/lcd 는 컬럼 없으면 400.

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS lcd_power        BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS lcd_quiet_start  TIME,
    ADD COLUMN IF NOT EXISTS lcd_quiet_end    TIME,
    ADD COLUMN IF NOT EXISTS lcd_saver        JSONB,
    ADD COLUMN IF NOT EXISTS lcd_image_key    TEXT,
    ADD COLUMN IF NOT EXISTS lcd_image_set_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lcd_updated_at   TIMESTAMPTZ;

COMMENT ON COLUMN public.devices.lcd_power
    IS 'LCD 수동 전원(백라이트). lcd_power ACK ok 시 확정. 소등 시간대와 독립 — false 면 시간대 무관하게 꺼짐';
COMMENT ON COLUMN public.devices.lcd_quiet_start
    IS 'LCD 소등 시작(KST 벽시계). lcd_quiet ACK ok 시 확정, lcd_quiet_clear 시 NULL. start>end 면 자정 넘는 구간';
COMMENT ON COLUMN public.devices.lcd_quiet_end
    IS 'LCD 소등 끝(KST 벽시계). lcd_quiet_start 와 쌍';
COMMENT ON COLUMN public.devices.lcd_saver
    IS '화면보호기 {enabled, image_sec, info_sec, show_offline_icon}. lcd_saver ACK ok 시 확정. NULL=미설정(기기 기본: 꺼짐)';
COMMENT ON COLUMN public.devices.lcd_image_key
    IS '화면보호기 이미지 R2 키 접두 lcd/{device_uuid}/{sha8} (.rgb565 기기용 + .png 미리보기). lcd_image ACK ok 시 확정, lcd_image_clear 시 NULL';
COMMENT ON COLUMN public.devices.lcd_image_set_at
    IS '화면보호기 이미지가 기기 플래시에 저장됨(lcd_image ACK ok 시각). lcd_image_clear 시 NULL';
COMMENT ON COLUMN public.devices.lcd_updated_at
    IS '위 lcd_power/quiet/saver/image 중 마지막 확정 시각';
