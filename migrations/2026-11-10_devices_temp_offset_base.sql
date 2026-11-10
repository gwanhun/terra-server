-- 2026-11-10: devices.temp_offset_base_c — 보드별 공장 기본 온도 보정(기기 NVS terra/t_base, telemetry 보고)
--
-- 배경(specs/stage-t-temp-offset-base-per-device.md): Stage S 는 공장 기본을 서버 env 하나(-1.5)로 전 기기 공통
-- 처리해, 운영자가 어떤 보드를 콘솔에서 -3 으로 보정하면 그 보드 사용자 앱에 -1.5 가 보였다. 이제 기기가 자기 base 를
-- NVS 에 갖고 telemetry `temp_offset_base_c` 로 보고하면 브리지가 이 컬럼에 저장하고, 사용자값(temp_offset_user_c)은
-- `temp_offset_c − 이 값` 으로 계산한다. 세 컬럼은 같은 UPDATE(쌍). 운영자 명령 `set_temp_base` 의 ACK ok 때도 확정.
-- 앱은 이 컬럼을 읽지 않는다(사용자값만). NULL = 미보고(구 펌웨어) → 서버는 env TEMP_OFFSET_BASE_C(-1.5) 로 폴백.
-- numeric(4,1): 운영자가 0.1 단위로 넣는다. 범위 ±10 (펌웨어 CALIB_MAX_ABS).
--
-- ⚠️ 코드 배포 **전** 적용(브리지는 컬럼 누락을 10분간 빼고 쓰지만 첫 1회는 UPDATE 재시도가 난다). 적용 후
-- MIGRATIONS_APPLIED.md 에 기록. 배포 순서: 이 파일 → 서버(API·브리지 둘 다 재시작) → nano 펌웨어 OTA.
--
-- 사전 확인:  SELECT column_name FROM information_schema.columns
--             WHERE table_schema='public' AND table_name='devices' AND column_name='temp_offset_base_c';  -- 0행
-- 사후 확인:  같은 쿼리 1행, data_type numeric, numeric_precision 4, numeric_scale 1, is_nullable YES

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS temp_offset_base_c NUMERIC(4, 1);

COMMENT ON COLUMN public.devices.temp_offset_base_c
    IS '보드 공장 기본 온도 보정(℃, 0.1 단위). 기기 NVS terra/t_base 를 telemetry temp_offset_base_c 로 보고한 최신값. temp_offset_user_c = temp_offset_c − 이 값. 운영자 set_temp_base 로 변경(기기가 적용값도 같이 옮겨 사용자값 불변). NULL=미보고(구 펌웨어, 서버 env -1.5 폴백) — Stage T 2026-11-10';
