-- 2026-10-01: devices.temp_offset_c — 기기가 지금 적용 중인 온도 보정값(펌웨어 보고)
--
-- 배경: set_temp_offset 명령(2026-09-20)은 기기 NVS 에만 저장되고 어디에도 보고되지 않아,
-- "이 기기 보정이 지금 얼마지?" 를 알 길이 commands 이력뿐이었다. 그런데 이력은 "보냈다"지
-- "적용 중" 이 아니다 — 보드 교체·flash erase 뒤엔 NVS 가 빌드 기본값(-1.5℃,
-- CONFIG_APP_TEMP_OFFSET_MDEG)으로 돌아가 DB 이력과 어긋난다.
-- 펌웨어 2026-10-01+ 가 telemetry 에 temp_offset_c 를 실어 보내고, 서버가 최신값을 여기 둔다
-- (sys_state 와 같은 "최신값 1개" 성격. telemetry 행에는 안 넣음 — 3초마다 쌓이므로).
-- NULL = 구 펌웨어(미보고). 콘솔은 그때 commands 의 마지막 set_temp_offset 값을 대신 보여준다.
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. 코드보다 먼저 적용할 것 — 없어도 _write_heartbeat 가
-- 이 컬럼만 빼고 쓰므로 온라인 표시는 살지만, 보정값은 저장되지 않는다.

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS temp_offset_c REAL;

COMMENT ON COLUMN public.devices.temp_offset_c
    IS '기기가 지금 적용 중인 온도 보정(℃, 측정값에 더함). 펌웨어 2026-10-01+ telemetry temp_offset_c 최신값. NULL=미보고(구 펌웨어)';
