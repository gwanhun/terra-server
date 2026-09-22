-- ============================================================================
-- ⚠️ 이 파일은 프로덕션과 어긋나 있다 — 그대로 재적용하지 말 것 (2026-09-22)
-- ----------------------------------------------------------------------------
-- 대상 함수: redesign_save_pet_v1, redesign_delete_pet_v1
-- 앱팀이 2026-09-22 운영 DB 에서 이 함수들의 '구성 변경' 오류 코드를
-- 40001(serialization_failure) → PT409 로 바꿨다. 이 파일에는 아직 40001 이 남아 있고,
-- 전부 CREATE OR REPLACE FUNCTION 이라 **재적용하면 그 수정이 조용히 되돌아간다.**
--
-- 왜 바꿨나: PostgREST 는 40001 을 일시적 충돌로 보고 트랜잭션을 자동 재시도한다.
-- 앱이 보낸 expected 구성이 실제와 다를 때는 재시도해도 조건이 안 바뀌어 무한 루프가 됐다
-- (운영 로그 24시간 약 520만 건, 분당 약 6,000건 / PostgREST 연결 4개 점유).
-- PT409 는 재시도 없이 HTTP 409 로 즉시 응답한다.
--
-- 권위 있는 SQL: tera-ai-flutter@main supabase/migrations/20260922_redesign_conflict_errcode.sql
-- 이 레포 사본의 동기화는 그 파일을 받은 뒤에 한다(추측으로 고치면 새 drift 가 생긴다).
--
-- DB 재구축·복구로 이 번들을 다시 붙여야 한다면, 01~06 적용 후 반드시 위 20260922
-- 마이그레이션을 이어서 적용할 것.
-- ============================================================================
-- ============================================================================
-- 앱 팀 재설계 번들 04/06 — redesign_pets
-- 출처: S-Soo100/tera-ai-flutter @ bb430fa  supabase/drafts/20260915_redesign_pets.sql
--       (SQL 최종 변경 커밋 d635785, 격리 검증 verify_redesign_sql.py exit 0)
-- 검토: docs/BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md (B1~B5·N2 반영 확인)
-- 처리: 원본의 검증기용 BEGIN/ROLLBACK 을 벗기고 BEGIN … COMMIT 으로 감쌈. 내용은 원본 그대로.
-- 순서: 01 → 06 반드시 순서대로. 실패하면 그 파일만 롤백되므로 원인 수정 후 같은 파일부터 재실행.
-- ============================================================================
BEGIN;

-- REVIEW ONLY, NOT DEPLOYED. Read-only production schema snapshot checked
-- 2026-09-15: pets columns/owner/group verified; media.pet_id ON DELETE CASCADE
-- and pet_events.pet_id ON DELETE CASCADE: use a profile tombstone instead.
-- Original pet/media/event rows remain; normal SELECT hides tombstoned profiles.
-- Dependencies: assignment_history + redesign_groups drafts, shared owner lock,
-- all legacy relationship writers migrated to the lock/history protocol.
-- No app fallback to separate profile UPDATE, assignment RPC, or DELETE.

-- Restrictive policy ANDs with existing permissive ALL/SELECT owner policies.
-- No existing direct-write grants or policies are changed by this draft.
DROP POLICY IF EXISTS redesign_active_pets_select ON public.pets;
CREATE POLICY redesign_active_pets_select ON public.pets AS RESTRICTIVE
  FOR SELECT TO authenticated USING (deleted_at IS NULL);
CREATE OR REPLACE FUNCTION public.redesign_save_pet_v1(
  p_pet jsonb,p_group_id uuid,p_expected_group_id uuid,p_request_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE owner uuid:=public.redesign_require_owner_lock(); pet_key uuid:=(p_pet->>'id')::uuid;
  old_group uuid; old_name text; old_owner uuid; old_deleted_at timestamptz; wanted text; result jsonb;
  prior public.redesign_group_requests%ROWTYPE;
  payload jsonb:=jsonb_build_object('pet',p_pet,'group',p_group_id,'expected',p_expected_group_id);
  changed_at timestamptz:=clock_timestamp(); is_existing boolean;
BEGIN
  PERFORM public.redesign_assert_history_helper();
  IF p_request_id IS NULL OR pet_key IS NULL OR jsonb_typeof(p_pet) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'invalid pet request' USING ERRCODE='22023'; END IF;
  SELECT * INTO prior FROM public.redesign_group_requests WHERE user_id=owner AND request_id=p_request_id;
  IF FOUND THEN
    IF prior.operation<>'pet_save' OR prior.payload<>payload THEN
      RAISE EXCEPTION 'idempotency key conflict' USING ERRCODE='22023'; END IF;
    RETURN prior.result;
  END IF;
  SELECT user_id,enclosure_id,name,deleted_at INTO old_owner,old_group,old_name,old_deleted_at FROM public.pets WHERE id=pet_key FOR UPDATE;
  is_existing:=FOUND;
  IF is_existing AND old_owner IS DISTINCT FROM owner THEN
    RAISE EXCEPTION 'pet not owned' USING ERRCODE='42501'; END IF;
  IF is_existing AND old_deleted_at IS NOT NULL THEN
    RAISE EXCEPTION 'deleted pet cannot be edited or resurrected' USING ERRCODE='40001'; END IF;
  IF old_group IS DISTINCT FROM p_expected_group_id THEN
    RAISE EXCEPTION 'membership changed' USING ERRCODE='40001'; END IF;
  IF p_group_id IS NOT NULL THEN
    PERFORM 1 FROM public.enclosures WHERE id=p_group_id AND owner_id=owner FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'group not owned' USING ERRCODE='42501'; END IF;
    -- Do not silently evict another pet. Group editor handles explicit moves.
    IF EXISTS(SELECT 1 FROM public.pets WHERE enclosure_id=p_group_id AND deleted_at IS NULL AND id<>pet_key) THEN
      RAISE EXCEPTION 'group already has a pet' USING ERRCODE='40001'; END IF;
  END IF;
  IF is_existing AND p_pet->>'name'=old_name THEN wanted:=old_name;
  ELSE
    wanted:=public.redesign_validate_name(p_pet->>'name');
    IF EXISTS(SELECT 1 FROM public.pets WHERE user_id=owner AND deleted_at IS NULL AND id<>pet_key AND btrim(name)=wanted) THEN
      RAISE EXCEPTION 'duplicate pet name' USING ERRCODE='23505'; END IF;
  END IF;
  IF nullif(p_pet->>'species_id','') IS NULL OR nullif(p_pet->>'species_name','') IS NULL
     OR coalesce(p_pet->>'sex','unknown') NOT IN ('male','female','unknown') THEN
    RAISE EXCEPTION 'invalid profile' USING ERRCODE='22023'; END IF;
  IF is_existing THEN
    UPDATE public.pets SET name=wanted,species_id=p_pet->>'species_id',species_name=p_pet->>'species_name',
      morph=p_pet->>'morph',sex=coalesce(p_pet->>'sex','unknown'),birth_date=(p_pet->>'birth_date')::date,
      adoption_date=(p_pet->>'adoption_date')::date,weight=(p_pet->>'weight')::numeric,
      avatar_url=p_pet->>'avatar_url',memo=p_pet->>'memo',enclosure_id=p_group_id,updated_at=changed_at
      WHERE id=pet_key AND user_id=owner;
  ELSE
    INSERT INTO public.pets(id,user_id,name,species_id,species_name,morph,sex,birth_date,adoption_date,
      weight,avatar_url,memo,enclosure_id,created_at,updated_at)
    VALUES(pet_key,owner,wanted,p_pet->>'species_id',p_pet->>'species_name',p_pet->>'morph',
      coalesce(p_pet->>'sex','unknown'),(p_pet->>'birth_date')::date,(p_pet->>'adoption_date')::date,
      (p_pet->>'weight')::numeric,p_pet->>'avatar_url',p_pet->>'memo',p_group_id,changed_at,changed_at);
  END IF;
  PERFORM public.redesign_reconcile_assignments(owner,changed_at);
  PERFORM public.redesign_cleanup_empty_groups(owner,ARRAY[old_group]);
  result:=jsonb_build_object('pet_id',pet_key);
  INSERT INTO public.redesign_group_requests VALUES(owner,p_request_id,'pet_save',payload,result,now());
  RETURN result;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_delete_pet_v1(
  p_pet_id uuid,p_expected_group_id uuid,p_request_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE owner uuid:=public.redesign_require_owner_lock(); actual uuid;
  original_owner uuid; original_deleted_at timestamptz; changed_at timestamptz:=clock_timestamp();
  prior public.redesign_group_requests%ROWTYPE; result jsonb;
  payload jsonb:=jsonb_build_object('pet',p_pet_id,'expected',p_expected_group_id);
BEGIN
  PERFORM public.redesign_assert_history_helper();
  IF p_pet_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'invalid deletion request' USING ERRCODE='22023'; END IF;
  SELECT * INTO prior FROM public.redesign_group_requests WHERE user_id=owner AND request_id=p_request_id;
  IF FOUND THEN
    IF prior.operation<>'pet_delete' OR prior.payload<>payload THEN
      RAISE EXCEPTION 'idempotency key conflict' USING ERRCODE='22023'; END IF;
    RETURN prior.result;
  END IF;
  SELECT user_id,enclosure_id,deleted_at INTO original_owner,actual,original_deleted_at
    FROM public.pets WHERE id=p_pet_id FOR UPDATE;
  IF NOT FOUND OR original_owner IS DISTINCT FROM owner THEN
    RAISE EXCEPTION 'pet not owned' USING ERRCODE='42501'; END IF;
  IF original_deleted_at IS NOT NULL OR actual IS DISTINCT FROM p_expected_group_id THEN
    RAISE EXCEPTION 'membership changed or pet already deleted' USING ERRCODE='40001'; END IF;
  -- No physical deletion: pet profile, media and pet_events remain intact.
  UPDATE public.pets SET enclosure_id=NULL,deleted_at=changed_at,updated_at=changed_at
    WHERE id=p_pet_id AND user_id=owner;
  PERFORM public.redesign_reconcile_assignments(owner,changed_at);
  PERFORM public.redesign_cleanup_empty_groups(owner,ARRAY[actual]);
  result:=jsonb_build_object('deleted',true,'pet_id',p_pet_id);
  INSERT INTO public.redesign_group_requests VALUES(owner,p_request_id,'pet_delete',payload,result,now());
  RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.redesign_save_pet_v1(jsonb,uuid,uuid,uuid) FROM PUBLIC,anon;
REVOKE ALL ON FUNCTION public.redesign_delete_pet_v1(uuid,uuid,uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.redesign_save_pet_v1(jsonb,uuid,uuid,uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION public.redesign_delete_pet_v1(uuid,uuid,uuid) TO authenticated;

COMMIT;
