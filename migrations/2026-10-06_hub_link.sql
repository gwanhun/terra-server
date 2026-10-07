-- 2026-10-06: cameras.device_id — Terra Hub(카메라+센서 통합 보드) 링크
--
-- 배경: ESP32-P4 카메라 보드 한 대가 사육장 기기(DHT22·펌프·팬·조명) 역할까지 맡는다
--       (specs/stage-k-unified-hub.md). 앱·웹·RLS·Realtime 이 devices/cameras 두 테이블을
--       전제로 짜여 있어 테이블을 합치지 않고, 허브 1대 = cameras 행 1 + devices 행 1 로 둔다.
--       두 행은 같은 MQTT 자격증명(camera_id == device_id 텍스트, 같은 token_hash)을 쓴다.
-- 용도: 허브 재페어링 시 devices 행을 다시 찾아 토큰을 함께 갱신하고, 해제/삭제를 묶는다.
-- NULL = 순수 카메라(기존 전부).

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS device_id UUID REFERENCES public.devices(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_cameras_device ON public.cameras(device_id);

COMMENT ON COLUMN public.cameras.device_id IS
    'Terra Hub 짝 devices.id. 허브는 cameras 행 + devices 행이 한 보드(같은 MQTT 계정). NULL = 순수 카메라.';
