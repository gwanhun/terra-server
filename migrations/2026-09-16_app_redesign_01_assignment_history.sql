-- ============================================================================
-- 앱 팀 재설계 번들 01/06 — assignment_history
-- 출처: S-Soo100/tera-ai-flutter @ bb430fa  supabase/drafts/20260915_assignment_history.sql
--       (SQL 최종 변경 커밋 d635785, 격리 검증 verify_redesign_sql.py exit 0)
-- 검토: docs/BACKEND_HANDOFF_REPLY_SQL_DRAFTS_2026-09-16.md (B1~B5·N2 반영 확인)
-- 처리: 원본의 검증기용 BEGIN/ROLLBACK 을 벗기고 BEGIN … COMMIT 으로 감쌈. 내용은 원본 그대로.
-- 순서: 01 → 06 반드시 순서대로. 실패하면 그 파일만 롤백되므로 원인 수정 후 같은 파일부터 재실행.
-- ============================================================================
BEGIN;

-- REVIEW DRAFT ONLY. Not applied to any database. Remote DDL/RLS audit required.
-- Coordinate all group/device/camera/pet writers with Lee before deployment.
-- Run before redesign_groups.sql; commit only as a separately reviewed migration.

-- A removed profile remains available to its original activity/media records.
-- The application and restrictive SELECT policy hide tombstoned profiles.
alter table public.pets add column if not exists deleted_at timestamptz;
-- B3 (backend review 2026-09-16): account deletion must cascade like devices/
-- cameras/enclosures already do. Production pets_user_id_fkey had no ON DELETE.
alter table public.pets drop constraint if exists pets_user_id_fkey;
alter table public.pets add constraint pets_user_id_fkey
  foreign key (user_id) references auth.users(id) on delete cascade;
create table if not exists public.pet_camera_assignments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  pet_id uuid references public.pets(id) on delete set null,
  pet_identity uuid not null,
  -- B2 (backend review 2026-09-16): hard DELETE /cameras means "records too",
  -- so history rows go with it. Soft unlink never deletes the camera row.
  camera_id uuid not null references public.cameras(id) on delete cascade,
  group_id uuid references public.enclosures(id) on delete set null,
  start_at timestamptz,
  end_at timestamptz,
  origin text not null check (origin in ('recorded','legacy_inherited')),
  created_at timestamptz not null default now(),
  check (origin = 'legacy_inherited' or start_at is not null),
  check (end_at is null or start_at is null or end_at > start_at)
);
create unique index if not exists pet_camera_assignment_open
  on public.pet_camera_assignments(user_id,pet_identity,camera_id) where end_at is null;
create index if not exists pet_camera_assignment_read
  on public.pet_camera_assignments(user_id,pet_identity,created_at);
alter table public.pet_camera_assignments enable row level security;
drop policy if exists assignment_owner_read on public.pet_camera_assignments;
create policy assignment_owner_read on public.pet_camera_assignments
  for select to authenticated using (user_id=auth.uid());
revoke all on public.pet_camera_assignments from anon, authenticated;
grant select on public.pet_camera_assignments to authenticated;

-- Internal helper. Caller holds the shared owner advisory lock and mutates all
-- relationships in the SAME transaction. It is never exposed as an app RPC.
create or replace function public.redesign_reconcile_assignments(p_user_id uuid,p_at timestamptz)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  if p_user_id is null or p_at is null then raise exception 'Missing owner/time'; end if;
  -- Closed rows survive reassignment, pet deletion and removal of empty groups.
  update public.pet_camera_assignments a set end_at=greatest(p_at,coalesce(a.start_at,p_at)+interval '1 microsecond')
  where a.user_id=p_user_id and a.end_at is null and not exists (
    select 1 from public.pets p join public.cameras c on c.enclosure_id=p.enclosure_id
    where p.id=a.pet_id and c.id=a.camera_id and p.user_id=p_user_id
      and c.owner_id=p_user_id and p.enclosure_id=a.group_id and p.deleted_at is null);
  insert into public.pet_camera_assignments(user_id,pet_id,pet_identity,camera_id,group_id,start_at,origin)
  select p_user_id,p.id,p.id,c.id,p.enclosure_id,p_at,'recorded'
  from public.pets p join public.cameras c on c.enclosure_id=p.enclosure_id
  where p.user_id=p_user_id and c.owner_id=p_user_id and p.deleted_at is null
    and not exists (select 1 from public.pet_camera_assignments a
      where a.user_id=p_user_id and a.pet_identity=p.id and a.camera_id=c.id and a.end_at is null);
end $$;
revoke all on function public.redesign_reconcile_assignments(uuid,timestamptz) from public,anon,authenticated;

-- IMPORTANT: No automatic legacy backfill. Old nightly report queried all owned
-- cameras, not each pet. Keep /my-pets/reports unchanged. Import legacy_inherited
-- rows only from an audited pre-cutover scope; unknown starts remain NULL, never
-- pet.created_at. Newly registered animals never receive legacy rows.
-- Non-app writers (terra-server service_role, web console) cannot join the
-- auth.uid() owner lock. 20260916_relationship_triggers.sql calls this helper
-- from cameras/pets row triggers so every path (PATCH enclosure_id, REST
-- unlink, DELETE /enclosures SET NULL) keeps history current. Soft unlink is
-- deployed (POST /devices|cameras/{id}/unlink, 2026-09-16); hard DELETE stays
-- operator-only and cascades history by B2.

COMMIT;
