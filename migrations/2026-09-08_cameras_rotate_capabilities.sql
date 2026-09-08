-- 2026-09-08: 카메라 180° 회전 설정 + 카메라 capabilities (앱 핸드오프 rotate180 R4/R5)
--
-- rotate_180  : 선언적 설정(진실). 앱이 PATCH /cameras/{id} 로 변경하고, 서버가 MQTT
--               set_rotation 명령을 발행한다. 펌웨어는 텔레메트리로 현재값을 보고하고
--               서버가 DB 와 다르면 재발행 → 오프라인/유실/재부팅 모두 DB 값으로 수렴.
-- capabilities: 펌웨어가 MQTT 연결 후 텔레메트리로 보고. NULL = 구 펌웨어(미보고).
--               예) {"rotate_180": true}. 앱은 capabilities.rotate_180 == true 일 때만
--               토글을 노출한다. devices.capabilities 와 달리 백필하지 않는다
--               (구 펌웨어 카메라에 토글이 떠서는 안 됨).

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS rotate_180 BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS capabilities JSONB;

COMMENT ON COLUMN public.cameras.rotate_180
    IS '영상 180° 회전(설치 방향 보정). 앱 설정값(진실), 펌웨어가 동기화';
COMMENT ON COLUMN public.cameras.capabilities
    IS '펌웨어 보고 능력 플래그 JSONB. 예 {"rotate_180":true}. NULL=미보고(구 펌웨어)';
