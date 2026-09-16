-- ============================================================================
-- 앱 팀 재설계 번들 05/06 — redesign_delete_group
-- 출처: S-Soo100/tera-ai-flutter @ bb430fa  supabase/drafts/20260915_redesign_delete_group.sql
--       (SQL 최종 변경 커밋 d635785, 격리 검증 verify_redesign_sql.py exit 0)
-- 검토: docs/BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md (B1~B5·N2 반영 확인)
-- 처리: 원본의 검증기용 BEGIN/ROLLBACK 을 벗기고 BEGIN … COMMIT 으로 감쌈. 내용은 원본 그대로.
-- 순서: 01 → 06 반드시 순서대로. 실패하면 그 파일만 롤백되므로 원인 수정 후 같은 파일부터 재실행.
-- ============================================================================
BEGIN;

-- REVIEW ONLY; NOT APPLIED. Depends on assignment_history + redesign_groups.
-- All relationship writers must share redesign_require_owner_lock/history.
-- Atomic group deletion: preserve registered members, ownership, clips and
-- favorites; close assignment intervals, remove only the enclosure anchor.
-- Existing motion_clips.enclosure_id FK becomes NULL, camera_id/R2 key survive.

CREATE OR REPLACE FUNCTION public.redesign_delete_group_v1(p_group_id uuid, p_request_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner uuid := public.redesign_require_owner_lock();
  payload jsonb := jsonb_build_object('group',p_group_id);
  prior public.redesign_group_requests%ROWTYPE; outcome jsonb;
BEGIN
  IF p_group_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'group and request required' USING ERRCODE='22023';
  END IF;
  SELECT * INTO prior FROM public.redesign_group_requests WHERE user_id=owner AND request_id=p_request_id;
  IF FOUND THEN
    IF prior.operation<>'delete-group' OR prior.payload<>payload THEN
      RAISE EXCEPTION 'idempotency key conflict' USING ERRCODE='22023';
    END IF;
    RETURN prior.result;
  END IF;
  PERFORM public.redesign_assert_history_helper();
  PERFORM 1 FROM public.enclosures WHERE id=p_group_id AND owner_id=owner FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'group not owned' USING ERRCODE='42501'; END IF;
  -- Lock all attached members before validating ownership. A corrupt mixed-owner
  -- group must not mutate a foreign row through the FK's ON DELETE SET NULL.
  PERFORM 1 FROM public.devices WHERE enclosure_id=p_group_id ORDER BY id FOR UPDATE;
  PERFORM 1 FROM public.cameras WHERE enclosure_id=p_group_id ORDER BY id FOR UPDATE;
  PERFORM 1 FROM public.pets WHERE enclosure_id=p_group_id ORDER BY id FOR UPDATE;
  IF EXISTS(SELECT 1 FROM public.devices WHERE enclosure_id=p_group_id AND owner_id IS DISTINCT FROM owner)
    OR EXISTS(SELECT 1 FROM public.cameras WHERE enclosure_id=p_group_id AND owner_id IS DISTINCT FROM owner)
    OR EXISTS(SELECT 1 FROM public.pets WHERE enclosure_id=p_group_id AND user_id IS DISTINCT FROM owner)
    OR EXISTS(SELECT 1 FROM public.motion_clips WHERE enclosure_id=p_group_id AND owner_id IS DISTINCT FROM owner) THEN
    RAISE EXCEPTION 'foreign member in group' USING ERRCODE='42501';
  END IF;
  UPDATE public.devices SET enclosure_id=NULL WHERE enclosure_id=p_group_id AND owner_id=owner;
  UPDATE public.cameras SET enclosure_id=NULL WHERE enclosure_id=p_group_id AND owner_id=owner;
  UPDATE public.pets SET enclosure_id=NULL WHERE enclosure_id=p_group_id AND user_id=owner;
  PERFORM public.redesign_reconcile_assignments(owner,clock_timestamp());
  DELETE FROM public.enclosures WHERE id=p_group_id AND owner_id=owner;
  outcome := jsonb_build_object('group_id',p_group_id,'deleted',true);
  INSERT INTO public.redesign_group_requests VALUES(owner,p_request_id,'delete-group',payload,outcome,now());
  RETURN outcome;
END $$;
REVOKE ALL ON FUNCTION public.redesign_delete_group_v1(uuid,uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.redesign_delete_group_v1(uuid,uuid) TO authenticated;

COMMIT;
