-- 2026-11-05: devices.temp_offset_user_c — 사용자 보정값(= 기기 보고 temp_offset_c − 공장 기본 TEMP_OFFSET_BASE_C)
--
-- 배경(specs/stage-s-temp-offset-base.md): 공장 기본 -1.5℃ 를 앱에서 숨기려면 앱이 읽는 값은
-- "사용자 보정값" 이어야 한다. 앱(0.166.0~)이 이미 기존 컬럼 temp_offset_c 를 절대값으로 읽고 있어
-- 의미를 바꿀 수 없으므로 별도 컬럼으로 둔다. 브리지가 telemetry 수신 때 temp_offset_c 와 **같은 UPDATE** 로
-- 쓴다(쌍). 앱은 PostgREST 직접 조회 + devices Realtime 으로 읽는다 — 기존 RLS 그대로.
-- numeric(4,1): 앱이 0.1 단위로 "저장 완료" 를 비교하므로 소수 1자리 고정. 범위는 -8.5~+11.5 (기기 ±10 − base).
-- NULL = 구 펌웨어(temp_offset_c 미보고)와 같은 규칙.
--
-- ⚠️ 코드 배포 **전** 적용. 없으면 _write_heartbeat 가 이 컬럼만 빼고 쓰므로 temp_offset_c 만 갱신돼
-- Realtime 에 두 값이 어긋난 이벤트가 나간다(스펙 함정 1). 적용 후 MIGRATIONS_APPLIED.md 에 기록.
--
-- 사전 확인:  SELECT column_name FROM information_schema.columns
--             WHERE table_schema='public' AND table_name='devices' AND column_name='temp_offset_user_c';  -- 0행
-- 사후 확인:  같은 쿼리 1행, data_type numeric, numeric_precision 4, numeric_scale 1, is_nullable YES,
--             SELECT count(*) FROM public.devices WHERE temp_offset_user_c IS NOT NULL;  -- 0 (브리지 배포 전)

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS temp_offset_user_c NUMERIC(4, 1);

COMMENT ON COLUMN public.devices.temp_offset_user_c
    IS '사용자 온도 보정(℃, 0.1 단위) = 기기 보고 temp_offset_c − 공장 기본(서버 TEMP_OFFSET_BASE_C, -1.5). 브리지가 temp_offset_c 와 같은 UPDATE 로 기록. NULL=미보고(구 펌웨어). 앱 신 계약(user_offset_c)이 읽는 값 — 2026-11-05';
