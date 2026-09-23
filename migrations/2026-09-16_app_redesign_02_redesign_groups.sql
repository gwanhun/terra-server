-- ============================================================================
-- ✅ 2026-09-23 운영과 동기화 — 오류 코드 40001 → PT409 (앱팀 2026-09-22 변경 반영)
-- ----------------------------------------------------------------------------
-- 대상 함수: redesign_save_group_v1, redesign_remove_group_member_v1
-- 앱팀이 2026-09-22 운영 DB 에서 이 함수들 안의 ERRCODE='40001' 을 전부 'PT409' 로
-- 치환했다(tera-ai-flutter supabase/migrations/20260922_redesign_conflict_errcode.sql —
-- pg_get_functiondef 로 본문을 읽어 regexp_replace 후 EXECUTE, 함수 본문은 그대로).
-- 이 사본에도 같은 치환을 적용했으므로 이제 그대로 재적용해도 운영과 일치한다.
--
-- 왜 PT409 인가: PostgREST 는 40001(serialization_failure)을 일시적 충돌로 보고 자동
-- 재시도한다. 앱의 expected 구성이 실제와 다를 때는 재시도해도 조건이 안 바뀌어 무한
-- 루프가 됐다(24시간 약 520만 건, 연결 4개 점유). PT409 는 재시도 없이 HTTP 409 즉시 응답.
-- 새 RPC 도 충돌·구성 변경류 오류는 PT409 를 쓴다(MIGRATIONS_APPLIED.md 규칙).
-- ============================================================================
-- ============================================================================
-- 앱 팀 재설계 번들 02/06 — redesign_groups
-- 출처: S-Soo100/tera-ai-flutter @ bb430fa  supabase/drafts/20260915_redesign_groups.sql
--       (SQL 최종 변경 커밋 d635785, 격리 검증 verify_redesign_sql.py exit 0)
-- 검토: docs/BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md (B1~B5·N2 반영 확인)
-- 처리: 원본의 검증기용 BEGIN/ROLLBACK 을 벗기고 BEGIN … COMMIT 으로 감쌈. 내용은 원본 그대로.
-- 순서: 01 → 06 반드시 순서대로. 실패하면 그 파일만 롤백되므로 원인 수정 후 같은 파일부터 재실행.
-- ============================================================================
BEGIN;

-- REVIEW-ONLY DRAFT. NOT APPLIED. Production public schema was read-only
-- audited from /private/tmp/redesign-remote-public-schema.sql on 2026-09-15.
-- Existing owner columns/types verified. These proposed RPCs/history are absent.
-- SQL execution and complete multi-writer rollout remain unverified.
-- Dependencies: 20260915_assignment_history.sql, verified owner/RLS columns,
-- all app/web/terra-server relationship writers routed through the same owner
-- lock + history helper. Without that rollout, this draft alone is NOT safe.
-- Never execute this file as an automated migration.
--
-- RPC contract (auth.uid is the only owner source):
-- save_group(group_id|null,name|null,device_id|null,camera_id|null,pet_id|null,
--            expected_members:[{kind,id,group_id}],request_id) -> {group_id}
-- name=null means server chooses the first free '사육 환경 N', starting at 1.
-- save/remove idempotency keys cannot be reused with different payloads.
-- Errors: 0A000 unsupported; 23505 duplicate name; PT409 changed membership;
-- 42501 missing ownership/auth; 22023 invalid input. No raw device/pet DELETE.
--
-- Unicode gap: PostgreSQL char_length is NOT a grapheme count. The temporary
-- helper below handles plain printable ASCII/precomposed Korean only and fails
-- closed for other text. Replace with a verified ICU/UAX29 implementation and
-- fixtures (family emoji/combining sequences) before enabling general Unicode.
-- Existing names are not renamed; unchanged legacy group names bypass the
-- new-name validator when only membership changes.


CREATE TABLE IF NOT EXISTS public.redesign_group_requests (
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  request_id uuid NOT NULL,
  operation text NOT NULL,
  payload jsonb NOT NULL,
  result jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, request_id)
);
ALTER TABLE public.redesign_group_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.redesign_group_requests FROM anon, authenticated;

CREATE TABLE IF NOT EXISTS public.redesign_group_counters (
  user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE, last_number bigint NOT NULL
);
ALTER TABLE public.redesign_group_counters ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.redesign_group_counters FROM anon,authenticated;

ALTER TABLE public.enclosures ADD COLUMN IF NOT EXISTS group_number bigint;
-- Soft unlink (deployed 2026-09-16): terra-server stamps unlinked_at via
-- POST /devices|cameras/{id}/unlink and revokes the MQTT account. Group writers
-- below treat unlinked rows as absent (B4); rows, clips and telemetry stay.
-- Columns already exist in production; IF NOT EXISTS keeps the draft replayable.
ALTER TABLE public.devices ADD COLUMN IF NOT EXISTS unlinked_at timestamptz;
ALTER TABLE public.cameras ADD COLUMN IF NOT EXISTS unlinked_at timestamptz;
-- Populate existing ordinals only after a catalog/duplicate audit. Never use
-- the current list index as a live ordinal. Never rename existing names here.

CREATE OR REPLACE FUNCTION public.redesign_require_owner_lock()
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner uuid := auth.uid();
BEGIN
  IF owner IS NULL THEN RAISE EXCEPTION 'authentication required' USING ERRCODE='42501'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('redesign-owner:' || owner::text,0));
  RETURN owner;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_validate_name(p_name text)
RETURNS text LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, public AS $$
DECLARE value text := btrim(p_name, U&' \0009\000A\000B\000C\000D\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000\FEFF');
BEGIN
  IF value IS NULL OR value='' THEN RAISE EXCEPTION 'name required' USING ERRCODE='22023'; END IF;
  IF value ~ '[^ -~가-힣]' THEN
    RAISE EXCEPTION 'Unicode grapheme validator pending' USING ERRCODE='0A000';
  END IF;
  IF char_length(value)>10 THEN RAISE EXCEPTION 'name too long' USING ERRCODE='22023'; END IF;
  RETURN value;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_assert_history_helper()
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF to_regprocedure('public.redesign_reconcile_assignments(uuid,timestamp with time zone)') IS NULL THEN
    RAISE EXCEPTION 'assignment history contract unavailable' USING ERRCODE='0A000';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_owned_member_group(p_owner uuid,p_kind text,p_id uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE found_group uuid; found_id uuid;
BEGIN
  IF p_kind='device' THEN
    SELECT id,enclosure_id INTO found_id,found_group FROM public.devices WHERE id=p_id AND owner_id=p_owner AND unlinked_at IS NULL FOR UPDATE;
  ELSIF p_kind='camera' THEN
    SELECT id,enclosure_id INTO found_id,found_group FROM public.cameras WHERE id=p_id AND owner_id=p_owner AND unlinked_at IS NULL FOR UPDATE;
  ELSIF p_kind='pet' THEN
    SELECT id,enclosure_id INTO found_id,found_group FROM public.pets WHERE id=p_id AND user_id=p_owner AND deleted_at IS NULL FOR UPDATE;
  ELSE RAISE EXCEPTION 'unknown member kind' USING ERRCODE='22023'; END IF;
  IF found_id IS NULL THEN RAISE EXCEPTION 'member not owned' USING ERRCODE='42501'; END IF;
  RETURN found_group;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_cleanup_empty_groups(p_owner uuid,p_group_ids uuid[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  -- Only empty group anchors. Cameras/devices/pets, clips and telemetry survive.
  DELETE FROM public.enclosures e WHERE e.owner_id=p_owner AND e.id=ANY(p_group_ids)
    AND NOT EXISTS(SELECT 1 FROM public.devices d WHERE d.enclosure_id=e.id)
    AND NOT EXISTS(SELECT 1 FROM public.cameras c WHERE c.enclosure_id=e.id)
    AND NOT EXISTS(SELECT 1 FROM public.pets p WHERE p.enclosure_id=e.id);
END $$;

CREATE OR REPLACE FUNCTION public.redesign_save_group_v1(
  p_group_id uuid,p_name text,p_device_id uuid,p_camera_id uuid,p_pet_id uuid,
  p_expected_members jsonb,p_request_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner uuid := public.redesign_require_owner_lock(); target uuid := p_group_id;
  payload jsonb := jsonb_build_object('group',p_group_id,'name',p_name,'device',p_device_id,
    'camera',p_camera_id,'pet',p_pet_id,'expected',p_expected_members);
  prior public.redesign_group_requests%ROWTYPE; entry jsonb; current_group uuid;
  wanted_name text; original_name text; candidate integer:=1; outcome jsonb; ordinal bigint;
  changed_at timestamptz := clock_timestamp(); actual jsonb; expected jsonb;
BEGIN
  PERFORM public.redesign_assert_history_helper();
  SELECT * INTO prior FROM public.redesign_group_requests WHERE user_id=owner AND request_id=p_request_id;
  IF FOUND THEN
    IF prior.operation<>'save' OR prior.payload<>payload THEN RAISE EXCEPTION 'idempotency key conflict' USING ERRCODE='22023'; END IF;
    RETURN prior.result;
  END IF;
  IF p_request_id IS NULL OR jsonb_typeof(p_expected_members)<>'array' OR
      (p_device_id IS NULL AND p_camera_id IS NULL AND p_pet_id IS NULL) THEN
    RAISE EXCEPTION 'invalid group' USING ERRCODE='22023';
  END IF;
  IF target IS NOT NULL THEN
    SELECT name INTO original_name FROM public.enclosures WHERE id=target AND owner_id=owner FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'group not owned' USING ERRCODE='42501'; END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('kind',s.kind,'id',s.id) ORDER BY s.kind,s.id),'[]') INTO actual
      FROM (SELECT 'device' kind,id FROM public.devices WHERE enclosure_id=target
        UNION ALL SELECT 'camera',id FROM public.cameras WHERE enclosure_id=target
        UNION ALL SELECT 'pet',id FROM public.pets WHERE enclosure_id=target AND deleted_at IS NULL) s;
    SELECT coalesce(jsonb_agg(jsonb_build_object('kind',v->>'kind','id',v->>'id') ORDER BY v->>'kind',v->>'id'),'[]') INTO expected
      FROM jsonb_array_elements(p_expected_members) v WHERE v->>'group_id'=target::text;
    IF actual<>expected THEN RAISE EXCEPTION 'group changed' USING ERRCODE='PT409'; END IF;
  END IF;
  FOR entry IN SELECT value FROM jsonb_array_elements(p_expected_members) LOOP
    current_group := public.redesign_owned_member_group(owner,entry->>'kind',(entry->>'id')::uuid);
    IF current_group IS DISTINCT FROM (entry->>'group_id')::uuid THEN
      RAISE EXCEPTION 'membership changed' USING ERRCODE='PT409';
    END IF;
  END LOOP;
  -- A caller cannot smuggle an unconfirmed selected member by omitting it.
  FOR entry IN SELECT v FROM jsonb_array_elements(jsonb_build_array(
    jsonb_build_object('kind','device','id',p_device_id),jsonb_build_object('kind','camera','id',p_camera_id),
    jsonb_build_object('kind','pet','id',p_pet_id))) v WHERE v->>'id' IS NOT NULL LOOP
    IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p_expected_members) e
      WHERE e->>'kind'=entry->>'kind' AND e->>'id'=entry->>'id') THEN
      RAISE EXCEPTION 'selected member missing expected state' USING ERRCODE='22023';
    END IF;
    PERFORM public.redesign_owned_member_group(owner,entry->>'kind',(entry->>'id')::uuid);
  END LOOP;
  IF p_name IS NULL AND target IS NULL THEN
    LOOP
      wanted_name := public.redesign_validate_name('사육 환경 '||candidate::text);
      EXIT WHEN NOT EXISTS(SELECT 1 FROM public.enclosures WHERE owner_id=owner AND btrim(name)=wanted_name);
      candidate:=candidate+1;
    END LOOP;
  ELSIF p_name=original_name THEN wanted_name:=original_name;
  ELSE wanted_name:=public.redesign_validate_name(p_name); END IF;
  IF (target IS NULL OR wanted_name IS DISTINCT FROM original_name) AND EXISTS(SELECT 1 FROM public.enclosures WHERE owner_id=owner AND id IS DISTINCT FROM target AND btrim(name)=wanted_name) THEN
    RAISE EXCEPTION 'duplicate group name' USING ERRCODE='23505';
  END IF;
  IF target IS NULL THEN
    INSERT INTO public.redesign_group_counters(user_id,last_number)
      VALUES(owner,(SELECT coalesce(max(group_number),0)+1 FROM public.enclosures WHERE owner_id=owner))
      ON CONFLICT(user_id) DO UPDATE SET last_number=redesign_group_counters.last_number+1
      RETURNING last_number INTO ordinal;
    INSERT INTO public.enclosures(owner_id,name,group_number)
      VALUES(owner,wanted_name,ordinal)
      RETURNING id INTO target;
  ELSE UPDATE public.enclosures SET name=wanted_name WHERE id=target AND owner_id=owner; END IF;
  UPDATE public.devices SET enclosure_id=NULL WHERE owner_id=owner AND enclosure_id=target AND id IS DISTINCT FROM p_device_id;
  UPDATE public.cameras SET enclosure_id=NULL WHERE owner_id=owner AND enclosure_id=target AND id IS DISTINCT FROM p_camera_id;
  UPDATE public.pets SET enclosure_id=NULL WHERE user_id=owner AND deleted_at IS NULL AND enclosure_id=target AND id IS DISTINCT FROM p_pet_id;
  UPDATE public.devices SET enclosure_id=target WHERE id=p_device_id AND owner_id=owner;
  UPDATE public.cameras SET enclosure_id=target WHERE id=p_camera_id AND owner_id=owner;
  UPDATE public.pets SET enclosure_id=target WHERE id=p_pet_id AND user_id=owner AND deleted_at IS NULL;
  PERFORM public.redesign_reconcile_assignments(owner,changed_at);
  PERFORM public.redesign_cleanup_empty_groups(owner,ARRAY(SELECT DISTINCT (e->>'group_id')::uuid
    FROM jsonb_array_elements(p_expected_members) e WHERE e->>'group_id' IS NOT NULL));
  outcome:=jsonb_build_object('group_id',target);
  INSERT INTO public.redesign_group_requests VALUES(owner,p_request_id,'save',payload,outcome,now());
  RETURN outcome;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_remove_group_member_v1(
  p_kind text,p_item_id uuid,p_expected_group_id uuid,p_request_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner uuid:=public.redesign_require_owner_lock(); actual uuid;
  payload jsonb:=jsonb_build_object('kind',p_kind,'id',p_item_id,'group',p_expected_group_id);
  prior public.redesign_group_requests%ROWTYPE; outcome jsonb:='{"removed":true}';
BEGIN
  PERFORM public.redesign_assert_history_helper();
  SELECT * INTO prior FROM public.redesign_group_requests WHERE user_id=owner AND request_id=p_request_id;
  IF FOUND THEN
    IF prior.operation<>'remove' OR prior.payload<>payload THEN RAISE EXCEPTION 'idempotency key conflict' USING ERRCODE='22023'; END IF;
    RETURN prior.result;
  END IF;
  actual:=public.redesign_owned_member_group(owner,p_kind,p_item_id);
  IF actual IS DISTINCT FROM p_expected_group_id THEN RAISE EXCEPTION 'membership changed' USING ERRCODE='PT409'; END IF;
  IF p_kind='device' THEN UPDATE public.devices SET enclosure_id=NULL WHERE id=p_item_id AND owner_id=owner;
  ELSIF p_kind='camera' THEN UPDATE public.cameras SET enclosure_id=NULL WHERE id=p_item_id AND owner_id=owner;
  ELSE UPDATE public.pets SET enclosure_id=NULL WHERE id=p_item_id AND user_id=owner; END IF;
  PERFORM public.redesign_reconcile_assignments(owner,clock_timestamp());
  PERFORM public.redesign_cleanup_empty_groups(owner,ARRAY[p_expected_group_id]);
  INSERT INTO public.redesign_group_requests VALUES(owner,p_request_id,'remove',payload,outcome,now());
  RETURN outcome;
END $$;

CREATE OR REPLACE FUNCTION public.redesign_rename_item_v1(p_kind text,p_item_id uuid,p_name text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner uuid:=public.redesign_require_owner_lock(); wanted text:=public.redesign_validate_name(p_name);
BEGIN
  IF p_kind NOT IN ('device','camera') THEN RAISE EXCEPTION 'invalid device kind' USING ERRCODE='22023'; END IF;
  PERFORM public.redesign_owned_member_group(owner,p_kind,p_item_id);
  IF EXISTS(SELECT 1 FROM public.devices WHERE owner_id=owner AND unlinked_at IS NULL AND btrim(name)=wanted AND NOT(p_kind='device' AND id=p_item_id))
    OR EXISTS(SELECT 1 FROM public.cameras WHERE owner_id=owner AND unlinked_at IS NULL AND btrim(name)=wanted AND NOT(p_kind='camera' AND id=p_item_id)) THEN
    RAISE EXCEPTION 'duplicate device name' USING ERRCODE='23505';
  END IF;
  IF p_kind='device' THEN UPDATE public.devices SET name=wanted WHERE id=p_item_id AND owner_id=owner;
  ELSE UPDATE public.cameras SET name=wanted WHERE id=p_item_id AND owner_id=owner; END IF;
  RETURN jsonb_build_object('id',p_item_id);
END $$;

-- N2 (backend review 2026-09-16): no unlink RPC. Device/camera unlink is
-- terra-server REST POST /devices|cameras/{id}/unlink (deployed 2026-09-16).
DROP FUNCTION IF EXISTS public.redesign_unlink_device_v1(text,uuid,uuid);

REVOKE ALL ON FUNCTION public.redesign_require_owner_lock() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.redesign_validate_name(text) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.redesign_assert_history_helper() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.redesign_owned_member_group(uuid,text,uuid) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.redesign_cleanup_empty_groups(uuid,uuid[]) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.redesign_save_group_v1(uuid,text,uuid,uuid,uuid,jsonb,uuid) FROM PUBLIC,anon;
REVOKE ALL ON FUNCTION public.redesign_remove_group_member_v1(text,uuid,uuid,uuid) FROM PUBLIC,anon;
REVOKE ALL ON FUNCTION public.redesign_rename_item_v1(text,uuid,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.redesign_save_group_v1(uuid,text,uuid,uuid,uuid,jsonb,uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION public.redesign_remove_group_member_v1(text,uuid,uuid,uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION public.redesign_rename_item_v1(text,uuid,text) TO authenticated;

-- Deliberate rollback: this is a review artifact, never a deployable migration.

COMMIT;
