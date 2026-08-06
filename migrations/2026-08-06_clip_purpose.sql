-- =====================================================================
-- motion_clips.clip_purpose — 촬영 목적(production | test) DB 계약 추가
-- =====================================================================
-- 배경: terra-server 개발 클립(test/)이 운영 motion_clips 에 등록돼 petcam-lab
--       라벨링 교차검수에 편입된 사건. 경로 문자열만 보는 패치가 아니라 촬영 목적을
--       DB 계약으로 고정한다. clip_purpose 는 "촬영 목적"이며, 라벨링 자격과 별개다
--       (petcam-lab: purpose=production + canonical terra-clips/clips/ + system
--        exclusion 없음 + media 존재 를 모두 요구).
-- 소유: motion_clips 는 terra-server 테이블 → 컬럼/제약/백필 모두 terra-server 소유.
--
-- ⚠️ 적용 순서: 이 마이그레이션을 **먼저 적용**한 뒤 writer(create_clip_meta) 배포.
--    (컬럼 없는 상태로 writer 만 뜨면 clip_purpose 포함 INSERT 가 실패)
--
-- ⚠️ 적용 전 preflight — namespace 분포가 예상과 같은지 먼저 확인:
--    (기대: test/ 655, terra-clips/clips/ 20733, research-quarantine/ 898,
--           research-excluded/ 46, deleted/ 4, 그 외 0)
--    가정: test/ 외 namespace 는 전부 "운영 촬영본(production)"이다. 그 외 prefix 가
--          나오면 Step 2.5 가드가 전체를 ROLLBACK 하므로 분류 확정 후 재실행할 것.
--
--    SELECT
--      CASE
--        WHEN r2_key LIKE 'test/%'               THEN 'test/'
--        WHEN r2_key LIKE 'terra-clips/clips/%'  THEN 'terra-clips/clips/'
--        WHEN r2_key LIKE 'research-quarantine/%' THEN 'research-quarantine/'
--        WHEN r2_key LIKE 'research-excluded/%'  THEN 'research-excluded/'
--        WHEN r2_key LIKE 'deleted/%'            THEN 'deleted/'
--        ELSE '(unknown)'
--      END AS namespace,
--      count(*)
--    FROM public.motion_clips
--    GROUP BY 1 ORDER BY 2 DESC;
-- =====================================================================

BEGIN;

-- Step 1. nullable 로 추가 (기본값 없음 → 백필을 강제)
ALTER TABLE public.motion_clips
    ADD COLUMN IF NOT EXISTS clip_purpose TEXT;

-- Step 2. 백필 — test/ → test, 운영 lineage allowlist → production
UPDATE public.motion_clips
    SET clip_purpose = 'test'
    WHERE clip_purpose IS NULL AND r2_key LIKE 'test/%';

UPDATE public.motion_clips
    SET clip_purpose = 'production'
    WHERE clip_purpose IS NULL AND (
           r2_key LIKE 'terra-clips/clips/%'
        OR r2_key LIKE 'research-quarantine/%'
        OR r2_key LIKE 'research-excluded/%'
        OR r2_key LIKE 'deleted/%'
    );

-- Step 2.5 가드 — 미분류(NULL) 행이 남으면 전체 중단.
-- 알 수 없는 prefix = 계약 밖 데이터 → 임의로 production 처리하지 않는다(fail-closed).
DO $$
DECLARE unmapped int;
BEGIN
    SELECT count(*) INTO unmapped FROM public.motion_clips WHERE clip_purpose IS NULL;
    IF unmapped > 0 THEN
        RAISE EXCEPTION 'clip_purpose 백필 미완: % 행이 허용 namespace 밖 prefix. 분류 확정 후 재실행', unmapped;
    END IF;
END;
$$;

-- Step 3. purpose ↔ prefix 계약을 CHECK 로 고정 (writer 의 _derive_clip_purpose 와 동일 allowlist).
--         production 을 `NOT LIKE test/` 로 열지 않고 명시적 허용 prefix 로 제한한다.
ALTER TABLE public.motion_clips
    ADD CONSTRAINT motion_clips_purpose_prefix_ck CHECK (
        (clip_purpose = 'test' AND r2_key LIKE 'test/%')
        OR (clip_purpose = 'production' AND (
               r2_key LIKE 'terra-clips/clips/%'
            OR r2_key LIKE 'research-quarantine/%'
            OR r2_key LIKE 'research-excluded/%'
            OR r2_key LIKE 'deleted/%'
        ))
    );

-- NOT NULL 승격 (백필·가드 통과 후)
ALTER TABLE public.motion_clips
    ALTER COLUMN clip_purpose SET NOT NULL;

-- 라벨링 필터가 purpose 로 스캔 → 조회 인덱스
CREATE INDEX IF NOT EXISTS idx_motion_clips_purpose
    ON public.motion_clips(clip_purpose);

COMMIT;

-- =====================================================================
-- 롤백 (역순)
-- =====================================================================
-- BEGIN;
-- DROP INDEX IF EXISTS public.idx_motion_clips_purpose;
-- ALTER TABLE public.motion_clips DROP CONSTRAINT IF EXISTS motion_clips_purpose_prefix_ck;
-- ALTER TABLE public.motion_clips DROP COLUMN IF EXISTS clip_purpose;
-- COMMIT;
