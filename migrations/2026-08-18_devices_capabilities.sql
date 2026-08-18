-- 2026-08-18: devices.capabilities (보드 능력 플래그) — 앱 핸드오프 §2
--
-- 배경: led_on + brightness 는 MOSFET 보드만 실효(릴레이 보드는 on/off). 앱이 보드 능력을
-- 알 방법이 없어 밝기 슬라이더를 못 켠다. board_type 단일 값 대신 capabilities JSONB 로
-- 확장 가능하게 둔다 (히터 미장착 보드 등도 한 필드로 커버).
--
-- 스키마(자유 JSON, 예):
--   {"board": "mosfet", "led_dimmable": true, "heater": true}
--   {"board": "relay",  "led_dimmable": false, "heater": true}
--
-- 값은 펌웨어가 페어링 시 보고(DevicePairRequest.capabilities). 기존 행은 릴레이 기본으로 백필.
-- 적용 후 MIGRATIONS_APPLIED.md 등에 기록.

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS capabilities JSONB;

-- 기존 디바이스 백필: 릴레이 보드 가정(가장 보수적 — 밝기 슬라이더 미노출).
-- 실제 MOSFET 보드는 재페어링/펌웨어 보고 시 갱신됨.
UPDATE public.devices
    SET capabilities = jsonb_build_object('board', 'relay', 'led_dimmable', false)
    WHERE capabilities IS NULL;
