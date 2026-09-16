-- ============================================================================
-- 앱 팀 재설계 번들 06/06 — clip_visibility
-- 출처: S-Soo100/tera-ai-flutter @ bb430fa  supabase/drafts/20260915_clip_visibility.sql
--       (SQL 최종 변경 커밋 d635785, 격리 검증 verify_redesign_sql.py exit 0)
-- 검토: docs/BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md (B1~B5·N2 반영 확인)
-- 처리: 원본의 검증기용 BEGIN/ROLLBACK 을 벗기고 BEGIN … COMMIT 으로 감쌈. 내용은 원본 그대로.
-- 순서: 01 → 06 반드시 순서대로. 실패하면 그 파일만 롤백되므로 원인 수정 후 같은 파일부터 재실행.
-- ============================================================================
BEGIN;

-- DRAFT ONLY. Not a migration and not deployed by this implementation.
-- Evidence: MotionClipRepository uses motion_clips.id/camera_id/owner_id;
-- existing community_clip_feed draft declares source_clip_id UUID. Validate
-- actual server policies before deployment. UUID id/owner_id were verified
-- from the linked public schema dump on 2026-09-15; table is not yet deployed.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_attribute
    WHERE attrelid = 'public.motion_clips'::regclass
      AND attname = 'id' AND atttypid = 'uuid'::regtype AND NOT attisdropped
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_attribute
    WHERE attrelid = 'public.motion_clips'::regclass
      AND attname = 'owner_id' AND atttypid = 'uuid'::regtype AND NOT attisdropped
  ) THEN
    RAISE EXCEPTION 'Review actual motion_clips UUID id/owner_id schema before applying';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.user_hidden_clips (
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  clip_id uuid NOT NULL REFERENCES public.motion_clips(id) ON DELETE CASCADE,
  hidden_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, clip_id)
);
ALTER TABLE public.user_hidden_clips ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_hidden_clips FORCE ROW LEVEL SECURITY;
REVOKE ALL ON public.user_hidden_clips FROM anon, authenticated;
GRANT SELECT, INSERT ON public.user_hidden_clips TO authenticated;

DROP POLICY IF EXISTS user_hidden_clips_read_own ON public.user_hidden_clips;
CREATE POLICY user_hidden_clips_read_own ON public.user_hidden_clips
  FOR SELECT TO authenticated USING ((SELECT auth.uid()) = user_id);
DROP POLICY IF EXISTS user_hidden_clips_insert_accessible ON public.user_hidden_clips;
CREATE POLICY user_hidden_clips_insert_accessible ON public.user_hidden_clips
  FOR INSERT TO authenticated WITH CHECK (
    (SELECT auth.uid()) = user_id
    AND EXISTS (
      SELECT 1 FROM public.motion_clips source_clip
      WHERE source_clip.id = user_hidden_clips.clip_id
        AND source_clip.owner_id = (SELECT auth.uid())
      -- Existing motion_clips SELECT RLS remains effective for this invoker.
    )
  );
-- No UPDATE/DELETE grants; duplicate insert uses ON CONFLICT DO NOTHING.
-- No trigger or policy modifies motion_clips, R2, activity, favorites or memos.

COMMIT;
