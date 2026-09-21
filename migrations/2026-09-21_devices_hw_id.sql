-- 2026-09-21: devices.hw_id — 보드 불변 하드웨어 ID (재페어링 중복 행 방지)
--
-- cameras 와 같은 문제가 사육장 기기(ESP32-S3)에도 있었다. WiFi 만 바꾸려고 재페어링하면
-- 서버가 같은 보드인지 알 방법이 없어 매번 새 devices 행이 생기고, 사용자 목록에
-- 유령 기기가 쌓인다. 페어링 본문에 하드웨어를 식별할 값이 없는 것이 원인
-- (name/species/firmware_ver/capabilities/enclosure_id 뿐).
--
-- 해결: 펌웨어가 efuse base MAC 12자리 hex 를 hw_id 로 보낸다.
--       /devices/pair 는 같은 owner 의 같은 hw_id 행이 있으면 INSERT 대신 UPDATE 한다.
--
-- 설계는 2026-09-21_cameras_hw_id.sql 과 동일하다:
--   - UNIQUE 는 (owner_id, hw_id) 부분 인덱스. 전역이면 중고 양도/반품 재판매가 막힌다.
--   - unlinked_at IS NULL 조건으로, 해제된 과거 행이 재등록을 막지 않게 한다.
--   - hw_id IS NULL(구 펌웨어) 행은 인덱스에서 빠져 서로 충돌하지 않는다.

ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS hw_id TEXT;

COMMENT ON COLUMN public.devices.hw_id IS
    '보드 불변 하드웨어 ID(efuse base MAC 12자리 hex, 예 A0B7651C2908). '
    'NULL=미보고(구 펌웨어). 재페어링 시 같은 보드를 알아보는 근거.';

CREATE UNIQUE INDEX IF NOT EXISTS devices_owner_hw_id_uniq
    ON public.devices (owner_id, hw_id)
    WHERE hw_id IS NOT NULL AND unlinked_at IS NULL;
