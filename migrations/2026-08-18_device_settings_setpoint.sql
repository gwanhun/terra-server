-- 2026-08-18: device_settings 단일 목표값(setpoint) — 앱 핸드오프 §5
--
-- 배경: device_settings 는 범위(target_temp_min/max, target_humid_min/max)만 있어
-- 앱이 쓰려는 "단일 목표 온/습도"가 없다. 유지형 가드(목표 도달 시 OFF)도 단일값을
-- 기준으로 하므로 target_temp_c / target_humidity_pct 를 추가한다.
-- 읽기/쓰기는 REST GET/PATCH /devices/{id}/settings (서버가 범위 검증 후 저장).
-- 적용 후 MIGRATIONS_APPLIED.md 등에 기록.

ALTER TABLE public.device_settings
    ADD COLUMN IF NOT EXISTS target_temp_c       FLOAT,   -- 목표 온도 단일값 (°C)
    ADD COLUMN IF NOT EXISTS target_humidity_pct FLOAT;   -- 목표 습도 단일값 (%RH)
