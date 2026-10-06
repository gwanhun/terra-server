-- 2026-10-07: devices.lcd_text — 기기 LCD 상단 밴드에 지금 떠 있는 커스텀 문구
--
-- 배경 (앱 요청 2026-10-06, tera-ai-flutter docs/handoffs/2026-10-06-lcd-text-server-request.md):
-- POST /devices/{id}/lcd 는 비트맵만 commands 에 넣어 원문이 어디에도 안 남았다. 앱은 보낸 문구를
-- 메모리에만 들고 있어, 재시작·다른 폰에서 LCD 와 앱 표시가 어긋났다(고객 문의).
-- 서버가 기기 ACK(result=ok) 시점에 확정한다 — 오프라인·만료·거부면 화면이 안 바뀌었으니 그대로.
-- NULL = 기본값("TERRA IOT") 표시 중 또는 이 컬럼 도입 전 설정분(미상). 앱은 그때 폰 저장값 → 기기 ID.
-- 소유자 SELECT RLS·Realtime 은 devices 에 이미 있어 컬럼 추가만으로 앱이 읽는다.
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. 코드보다 먼저 적용할 것 — 없으면 ACK 때 lcd_text UPDATE 만
-- 실패하고(로그) ack 처리 자체는 계속된다.

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS lcd_text TEXT,
    ADD COLUMN IF NOT EXISTS lcd_text_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN public.devices.lcd_text
    IS 'LCD 상단 밴드 커스텀 문구(원문). lcd_bitmap ACK ok 시 확정, lcd_clear ACK ok 시 NULL. NULL=기본값 또는 미상';
COMMENT ON COLUMN public.devices.lcd_text_updated_at
    IS 'lcd_text 를 마지막으로 확정한 시각(ACK 수신 시각)';
