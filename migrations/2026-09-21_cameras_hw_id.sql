-- 2026-09-21: cameras.hw_id — 보드 불변 하드웨어 ID (재페어링 중복 행 방지)
--
-- 문제: 페어링 요청에 하드웨어를 식별할 값이 하나도 없었다(name/model/firmware_ver/
--       resolution/fps/clip_sec/enclosure_id 뿐). 그래서 같은 보드가 WiFi 를 바꾸려고
--       재페어링하면 서버가 같은 기기인지 알 길이 없어 매번 새 cameras 행이 생겼고,
--       사육장 하나에 유령 카메라가 쌓였다(베타: 사육장 1개에 카메라 3행).
--
-- 해결: 펌웨어가 efuse base MAC 12자리 hex 를 hw_id 로 보낸다(2026-09-21 펌웨어).
--       재부팅·NVS 삭제·펌웨어 재설치에도 불변이라 물리 보드의 참 식별자가 된다.
--       /cameras/pair 는 같은 owner 의 같은 hw_id 행이 있으면 INSERT 대신 UPDATE 한다.
--
-- 주의: UNIQUE 는 (owner_id, hw_id) 부분 인덱스로 건다.
--   - 전역 UNIQUE 로 걸면 중고 양도/반품 재판매 시 다른 소유자가 같은 보드를 등록하지
--     못한다. 소유자별로만 유일하면 충분하다.
--   - unlinked_at IS NULL 조건을 넣어, 해제된 과거 행이 재등록을 막지 않게 한다.
--   - hw_id IS NULL(구 펌웨어) 행은 인덱스에서 빠져 서로 충돌하지 않는다.

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS hw_id TEXT;

COMMENT ON COLUMN public.cameras.hw_id IS
    '보드 불변 하드웨어 ID(efuse base MAC 12자리 hex, 예 30EDA0E22E80). '
    'NULL=미보고(구 펌웨어). 재페어링 시 같은 보드를 알아보는 근거.';

CREATE UNIQUE INDEX IF NOT EXISTS cameras_owner_hw_id_uniq
    ON public.cameras (owner_id, hw_id)
    WHERE hw_id IS NOT NULL AND unlinked_at IS NULL;
