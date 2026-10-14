-- 2026-10-14: OTA 타겟에 Terra Hub(hub_p4) 추가 — Stage K (specs/stage-k-unified-hub.md)
--
-- 허브는 cameras 행(model='esp32-p4-hub')이라 kind=camera 로 OTA 를 타지만, 펌웨어 이미지는
-- 카메라(firebeetle2_p4_yr030)와 다른 프로젝트(terra_hub_p4)다. 둘 다 esp32p4 라 chip_id 로는
-- 못 가르므로 release.target 을 분리해 카메라 bin 이 허브에, 허브 bin 이 카메라에 가는 걸 막는다.
-- backend/ota_service.target_for() 가 entity.model 로 기대 target 을 고른다.
--
-- 재실행 안전: 제약을 지우고 다시 건다(인라인 CHECK 의 자동 이름 = firmware_releases_target_check).

ALTER TABLE public.firmware_releases
    DROP CONSTRAINT IF EXISTS firmware_releases_target_check;
ALTER TABLE public.firmware_releases
    ADD CONSTRAINT firmware_releases_target_check
    CHECK (target IN ('camera_p4', 'device_nano', 'hub_p4'));

COMMENT ON COLUMN public.firmware_releases.target IS
    'camera_p4(firebeetle2_p4_yr030) | device_nano(terra-iot) | hub_p4(terra_hub_p4, model esp32-p4-hub). ota_service.target_for';
