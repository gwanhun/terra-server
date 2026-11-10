-- 2026-11-10: cameras 재등록 행 재사용 + 교차 계정 superseded (Stage U)
--
-- 배경(영상·카메라 DB 문제 정리 2026-11-09, 팀 결정 2026-11-10):
--   cameras 169행 중 활성 37행. 한 행이 "물리 카메라 1대"가 아니라 "등록 1회"가 됐다.
--   (1) 같은 계정이 해제 뒤 재등록하면 새 행 → 해제 행이 계속 쌓임.
--   (2) 다른 계정이 같은 보드(hw_id)를 등록하면 이전 계정 행이 활성으로 남음.
--
-- 결정:
--   (1) 같은 계정·같은 hw_id 재등록은 **가장 최근 해제 행을 되살린다**(unlinked_at=NULL, 설정 초기화).
--       → 컬럼 추가 없음. (owner_id, hw_id) 부분 UNIQUE(2026-09-21) 가 "계정당 보드 1활성행" 을 보장.
--   (2) 교차 계정은 페어링 요청 안에서 아무것도 바꾸지 않는다. POST /cameras/pair 는 JWT 만 있으면
--       되고 hw_id 는 본문 값이라, 남의 MAC 으로 페어링해 그 사람 카메라를 해제시킬 수 있기 때문.
--       대신 브리지 offline_monitor 가 "새 행(다른 계정) 온라인 + 이전 행 24시간 이상 오프라인" 을
--       보고 이전 행에 superseded_at 을 찍는다(목록에서만 숨김, 행·설정·영상 유지).
--       숨긴 행의 보드가 옛 자격증명으로 다시 접속하면(공격자가 위조 불가) superseded 를 풀고
--       새 행에 supersede_suspect_at 을 남긴다. superseded 30일 뒤 재접속이 없으면 MQTT 계정 해지.
--
-- 적용 순서: 코드 배포 **전**. list_cameras 가 superseded_at 을 필터하므로 컬럼이 없으면 400.

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS superseded_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_by         UUID REFERENCES public.cameras(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS supersede_suspect_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS mqtt_revoked_at       TIMESTAMPTZ;

COMMENT ON COLUMN public.cameras.superseded_at IS
    '다른 계정이 같은 hw_id 보드를 등록하고 이 행이 24시간 이상 오프라인이라 목록에서 숨긴 시각. '
    'NULL=정상. 행·설정·영상은 유지. 보드가 옛 자격증명으로 재접속하면 브리지가 NULL 로 되돌린다.';
COMMENT ON COLUMN public.cameras.superseded_by IS
    '이 행을 밀어낸(같은 hw_id, 다른 계정) 새 cameras 행. superseded_at 과 쌍.';
COMMENT ON COLUMN public.cameras.supersede_suspect_at IS
    '이 행이 밀어낸 옛 행의 보드가 다시 접속해 superseded 가 풀린 시각. '
    '남의 MAC 으로 페어링한 의심 행 표식(운영 확인용). NULL=정상.';
COMMENT ON COLUMN public.cameras.mqtt_revoked_at IS
    'superseded 30일 뒤 재접속이 없어 Mosquitto 계정을 해지한 시각. 이후 ACL 재생성에서 제외.';

-- offline_monitor 가 hw_id 로 묶어 스캔. 활성(unlinked_at IS NULL) 행만 대상.
CREATE INDEX IF NOT EXISTS idx_cameras_hw_id_active
    ON public.cameras (hw_id)
    WHERE hw_id IS NOT NULL AND unlinked_at IS NULL;
