-- 2026-08-18: telemetry.led 상태 — 앱 핸드오프 §4
--
-- 배경: telemetry 에 relay/fan/heater_state 는 있는데 LED 상태 컬럼이 없어, 앱이 LED 를
-- 로컬 플래그(_ledOn)로 추측 중 → 앱 재시작·다기기·예약 실행 후 실제와 어긋남.
-- relay/fan 과 동일하게 원본 telemetry 에 상태를 싣는다. (roll-up telemetry_1m 은 수치
-- 집계라 상태값 미포함 — relay/fan/heater_state 와 동일 취급.)
-- 값: led = 'ON' | 'OFF' | NULL, led_brightness = 0~100 (MOSFET 보드만, 릴레이는 NULL).
-- 적용 후 MIGRATIONS_APPLIED.md 등에 기록.

ALTER TABLE public.telemetry
    ADD COLUMN IF NOT EXISTS led            TEXT,       -- 'ON' | 'OFF' | NULL
    ADD COLUMN IF NOT EXISTS led_brightness SMALLINT;   -- 0~100 (MOSFET), 릴레이는 NULL
