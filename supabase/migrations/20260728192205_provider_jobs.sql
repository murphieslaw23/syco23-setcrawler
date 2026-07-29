-- Durable provider import jobs and cursors for SETCRAWLER v0.2.
create table import_jobs (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('youtube','soundcloud','freeteknomusic')),
  job_type text not null check (job_type in ('url_import','search_profile','crawl')),
  input_url text,
  search_profile_id uuid references search_profiles(id) on delete restrict,
  status text not null default 'queued'
    check (status in ('queued','processing','retry','completed','failed','blocked','dead_letter')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  started_at timestamptz,
  finished_at timestamptz,
  next_retry_at timestamptz,
  result_set_id uuid references sets(id) on delete set null,
  error_code text,
  error_message text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table provider_cursors (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('youtube','soundcloud','freeteknomusic')),
  cursor_key text not null,
  cursor_value text,
  last_success_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (source, cursor_key)
);

create index import_jobs_status_created_idx on import_jobs (status, created_at desc);
create index import_jobs_source_created_idx on import_jobs (source, created_at desc);
create index import_jobs_profile_idx on import_jobs (search_profile_id, created_at desc)
  where search_profile_id is not null;
create unique index import_jobs_profile_child_idx
  on import_jobs (
    (details->>'profile_job_id'),
    (details->>'source_id')
  )
  where job_type = 'url_import'
    and details ? 'profile_job_id'
    and details ? 'source_id';
create index sets_canonical_url_idx on sets (canonical_url);
create index sets_fingerprint_idx on sets ((raw_payload->>'duplicate_fingerprint'))
  where raw_payload ? 'duplicate_fingerprint';

alter table import_jobs enable row level security;
alter table provider_cursors enable row level security;

grant select, insert, update on table import_jobs to authenticated;
grant select, insert, update, delete on table provider_cursors to authenticated;

create schema if not exists private;
revoke all on schema private from public;
grant usage on schema private to authenticated;

create or replace function private.has_role(required_role text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.user_roles
    where user_id = (select auth.uid())
      and role = required_role
  );
$$;

revoke all on function private.has_role(text) from public;
grant execute on function private.has_role(text) to authenticated;

drop policy if exists "editors update reviewable sets" on sets;
drop policy if exists "admins all sets" on sets;
drop policy if exists "editors manage artists" on artists;
drop policy if exists "editors manage events" on events;
drop policy if exists "editors manage crews" on crews;
drop policy if exists "editors manage images" on images;
drop policy if exists "editors manage candidates" on field_candidates;
drop policy if exists "editors manage profiles" on search_profiles;
drop policy if exists "admins manage profiles" on search_profiles;
drop policy if exists "admins manage heuristics" on heuristic_config;
drop policy if exists "users read own role" on user_roles;
drop policy if exists "admins manage roles" on user_roles;

create policy "editors update reviewable sets" on sets
  for update to authenticated using (
    private.has_role('editor') or private.has_role('admin')
  ) with check (
    private.has_role('editor') or private.has_role('admin')
  );
create policy "admins all sets" on sets
  for all to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));
create policy "editors manage artists" on artists
  for all to authenticated using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors manage events" on events
  for all to authenticated using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors manage crews" on crews
  for all to authenticated using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors manage images" on images
  for all to authenticated using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors manage candidates" on field_candidates
  for all to authenticated using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "admins manage profiles" on search_profiles
  for all to authenticated using (private.has_role('admin'))
  with check (private.has_role('admin'));
create policy "admins manage heuristics" on heuristic_config
  for all to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));
create policy "users read own role" on user_roles
  for select to authenticated using (user_id = auth.uid() or private.has_role('admin'));
create policy "admins manage roles" on user_roles
  for all to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));

drop function if exists public.has_role(text);

create policy "authenticated read import jobs" on import_jobs
  for select to authenticated using (true);
create policy "editors create import jobs" on import_jobs
  for insert to authenticated with check (
    private.has_role('editor') or private.has_role('admin')
  );
create policy "admins complete import jobs" on import_jobs
  for update to authenticated using (private.has_role('admin')) with check (
    private.has_role('admin')
    and status in ('completed', 'failed', 'blocked', 'dead_letter')
  );
create policy "admins read provider cursors" on provider_cursors
  for select to authenticated using (private.has_role('admin'));
create policy "admins manage provider cursors" on provider_cursors
  for all to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));
