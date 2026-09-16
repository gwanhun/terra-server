-- 앱 팀 재설계 번들 적용 후 확인 (읽기 전용). 결과를 앱 팀 회신에 첨부한다.
-- 1) 함수명·시그니처 — 초안과 동일해야 함 (redesign_unlink_device_v1 은 없어야 함)
SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prosecdef AS security_definer
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname LIKE 'redesign_%'
 ORDER BY p.proname;
-- 기대: redesign_assert_history_helper, redesign_cleanup_empty_groups, redesign_delete_group_v1,
--       redesign_delete_pet_v1, redesign_owned_member_group, redesign_reconcile_assignments,
--       redesign_remove_group_member_v1, redesign_rename_item_v1, redesign_require_owner_lock,
--       redesign_save_group_v1, redesign_save_pet_v1, redesign_touch_assignments, redesign_validate_name

-- 2) 트리거 2개
SELECT tgname, tgrelid::regclass FROM pg_trigger WHERE tgname LIKE 'trg_%touch_assignments' ORDER BY 1;

-- 3) pets.user_id FK 가 CASCADE 하나뿐인지 (IF EXISTS 로 옛 제약이 살아남으면 탈퇴가 막힘)
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid = 'public.pets'::regclass AND contype = 'f' ORDER BY 1;

-- 4) 새 테이블·컬럼
SELECT table_name FROM information_schema.tables WHERE table_schema='public'
   AND table_name IN ('pet_camera_assignments','redesign_group_requests','redesign_group_counters','user_hidden_clips') ORDER BY 1;
SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='pets' AND column_name='deleted_at';
SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='enclosures' AND column_name='group_number';

-- 5) 앱 호출 권한 (authenticated 가 EXECUTE 를 가져야 하는 RPC 5개)
SELECT p.proname, has_function_privilege('authenticated', p.oid, 'EXECUTE') AS app_can_call
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname IN ('redesign_save_group_v1','redesign_remove_group_member_v1',
       'redesign_rename_item_v1','redesign_delete_group_v1','redesign_save_pet_v1','redesign_delete_pet_v1')
 ORDER BY 1;
