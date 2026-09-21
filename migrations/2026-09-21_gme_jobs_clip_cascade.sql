-- 2026-09-21: gme_jobs.clip_id → motion_clips(id) 에 ON DELETE CASCADE 추가
--
-- 증상: 카메라 삭제(DELETE /cameras/{id})가 500. 카메라 → motion_clips 는 CASCADE 인데
--       gme_jobs 가 그 클립을 참조하면서 CASCADE 가 없어 cascade 삭제가 막힌다.
--         update or delete on table "motion_clips" violates foreign key constraint
--         "gme_jobs_clip_id_fkey" on table "gme_jobs"
--       클립 단건 삭제(DELETE motion_clips)도 같은 이유로 실패한다.
--
-- 판단: gme_jobs 는 클립 분석 작업 큐다. 원본 클립이 사라지면 그 작업 행은 의미가 없으므로
--       CASCADE 가 맞다. 작업 이력을 남겨야 한다면 CASCADE 대신 clip_id 를 NULL 허용으로
--       바꾸고 ON DELETE SET NULL 을 쓸 것 (아래 대안 참고).
--
-- ⚠️ gme_jobs 는 이 레포가 만든 테이블이 아니다(분석 파이프라인 소유).
--    적용 전 그쪽 담당자와 합의할 것.
--
-- 【2026-09-21 적용 후 후속 발견】 이것만으로는 카메라 하드 삭제가 되지 않는다.
--   motion_clips 를 참조하는 테이블이 45개 있다(gme_runs, clip_labeling_sessions,
--   motion_clip_gt_heads, motion_clip_consensus, motion_clip_eval_samples, behavior_labels …).
--   전부 CASCADE 로 바꾸면 클립 삭제 시 라벨링·정답(GT)·합의 데이터까지 사라진다.
--   → 클립이 쌓인 기기·카메라는 하드 삭제 대신 소프트 해제(POST /{kind}/{id}/unlink)를 쓴다.
--     하드 삭제는 클립이 없는(갓 등록한) 기기에만 안전하다.

ALTER TABLE public.gme_jobs
    DROP CONSTRAINT IF EXISTS gme_jobs_clip_id_fkey;

ALTER TABLE public.gme_jobs
    ADD CONSTRAINT gme_jobs_clip_id_fkey
    FOREIGN KEY (clip_id) REFERENCES public.motion_clips(id) ON DELETE CASCADE;

-- 대안 (작업 이력 보존이 필요할 때):
--   ALTER TABLE public.gme_jobs ALTER COLUMN clip_id DROP NOT NULL;
--   ALTER TABLE public.gme_jobs DROP CONSTRAINT IF EXISTS gme_jobs_clip_id_fkey;
--   ALTER TABLE public.gme_jobs ADD CONSTRAINT gme_jobs_clip_id_fkey
--       FOREIGN KEY (clip_id) REFERENCES public.motion_clips(id) ON DELETE SET NULL;
