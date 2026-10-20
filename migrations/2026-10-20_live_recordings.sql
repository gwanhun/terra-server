-- 2026-10-20: 라이브 직접 녹화 (앱 요청 2026-10-20, 회신 docs/BACKEND_HANDOFF_REPLY_LIVE_RECORDING_2026-10-20.md)
create table if not exists public.live_recordings (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  camera_id     uuid references public.cameras(id) on delete set null,
  client_id     uuid not null,
  status        text not null default 'pending' check (status in ('pending', 'ready')),
  started_at    timestamptz not null,
  duration_ms   int not null,
  size_bytes    bigint not null,
  width         int,
  height        int,
  r2_key        text not null,
  thumbnail_key text,
  has_thumbnail boolean generated always as (thumbnail_key is not null) stored,
  created_at    timestamptz not null default now(),
  unique (user_id, client_id)
);

alter table public.live_recordings enable row level security;

drop policy if exists "live_recordings select own" on public.live_recordings;
create policy "live_recordings select own" on public.live_recordings
  for select to authenticated using (user_id = auth.uid());
-- INSERT/UPDATE/DELETE 정책 없음 = 서버(service_role)만.
grant select on public.live_recordings to authenticated;

create index if not exists live_recordings_user_started_idx
  on public.live_recordings (user_id, started_at desc);
create index if not exists live_recordings_status_created_idx
  on public.live_recordings (status, created_at);
