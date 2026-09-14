-- 2026-09-07: telemetry.fan2 상태 — 냉각팬 (두 번째 팬)
--
-- 배경: 펌웨어는 팬 2채널(fan=팬, fan2=냉각팬)을 이미 지원하고 telemetry 에
-- "fan2": "ON"|"OFF" 를 싣지만, 서버 telemetry 테이블에 컬럼이 없어 버려지고 있었다.
-- fan/relay/led 와 동일하게 원본 telemetry 에 상태를 싣는다. (roll-up telemetry_1m 은
-- 수치 집계라 상태값 미포함 — fan/relay/heater_state 와 동일 취급.)
-- 값: fan2 = 'ON' | 'OFF' | NULL (구 펌웨어 미보고).

ALTER TABLE telemetry
    ADD COLUMN IF NOT EXISTS fan2 TEXT;   -- 'ON' | 'OFF' | NULL (냉각팬)
