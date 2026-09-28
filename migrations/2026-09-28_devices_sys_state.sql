-- 2026-09-28: devices.sys_state (IoT 보드 시스템 진단 — 카메라 clip_stats.sys 와 같은 내용)
--
-- 배경: 카메라는 heartbeat 의 uptime/reset/heap/rssi 를 cameras.clip_stats.sys 에 얹어 콘솔에
-- "up 5m · reset SW:mqtt_reboot · heap · rssi" 로 보여주는데, IoT 보드(terra-iot-nano /
-- supermini)는 같은 필드를 보낼 곳이 없었다. telemetry 행에 붙이면 3초마다 쌓이므로
-- devices 에 최신값 1개만 둔다.
--
-- 스키마(자유 JSON, 펌웨어 2026-09-28+ 가 telemetry 에 실어 보냄):
--   {"uptime_s": 300, "reset": "SW:mqtt_reboot", "heap": 19485000, "rssi": -36}
-- 구 펌웨어(uptime_sec 키 없음)는 NULL 유지.

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS sys_state JSONB;

COMMENT ON COLUMN public.devices.sys_state
    IS '펌웨어 telemetry 의 uptime_sec/reset/free_heap/wifi_rssi 최신값. 예 {"uptime_s":300,"reset":"POWERON","heap":190000,"rssi":-40}. NULL=미보고(구 펌웨어)';
