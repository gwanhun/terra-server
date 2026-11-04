-- 2026-11-04: devices.lcd_screen_on / lcd_override_until — 기기가 보고하는 LCD 실제 화면 상태
-- (specs/stage-n-lcd-saver-power.md N-4, 앱팀 요청 2026-11-03 "소등 중 수동 조작 override")
--
-- lcd_power 는 "사용자가 마지막으로 보낸 전원 설정"이고, 화면이 실제로 켜져 있는지는 소등 시간대·override
-- 때문에 그것만으로 알 수 없다. 펌웨어(capabilities.lcd_override 이상)가 telemetry 의 `lcd` 블록으로
-- {on, override_until, image} 를 3초마다 보고하고, 브리지는 **바뀔 때만** 여기 반영한다(capabilities 와 같은 패턴).
-- 구 펌웨어는 블록을 안 보내므로 NULL = 미상.
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. 코드보다 먼저 적용할 것 — 없으면 브리지가 PGRST204 를 보고
-- 그 컬럼만 10분간 빼고 쓴다(경고 1회). GET /devices/{id}/lcd 는 컬럼 없으면 500(postgrest APIError).

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS lcd_screen_on      BOOLEAN,
    ADD COLUMN IF NOT EXISTS lcd_override_until TIMESTAMPTZ;

COMMENT ON COLUMN public.devices.lcd_screen_on
    IS 'LCD 가 실제로 켜져 있는지 — 펌웨어 telemetry lcd.on 보고값(바뀔 때만 갱신). NULL = 구 펌웨어·미보고. 기기 오프라인이면 마지막 보고값';
COMMENT ON COLUMN public.devices.lcd_override_until
    IS '소등 시간대 중 수동 전원 조작이 유지되는 끝 시각(다음 예약 경계) — 펌웨어 보고값. NULL = override 없음';
