-- v0.6 service-role-only cleanup queue and append-only attempt tombstones.
-- Private object and multipart identity never leave the server boundary.

begin;

create table public.creator_upload_cleanup_jobs (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null unique
    references public.creator_upload_sessions(id) on delete restrict,
  reason text not null check (
    reason in (
      'user_abort', 'admin_abort', 'expired',
      'rights_denied', 'verification_failed'
    )
  ),
  status text not null default 'queued' check (
    status in ('queued', 'processing', 'retry', 'completed', 'dead_letter')
  ),
  object_key text check (
    object_key is null
    or object_key ~ '^objects/[0-9a-f]{2}/[0-9a-f]{32}$'
  ),
  storage_upload_id text check (
    storage_upload_id is null
    or char_length(storage_upload_id) between 1 and 2048
  ),
  requested_by text not null
    check (char_length(requested_by) between 1 and 300),
  attempt_count integer not null default 0
    check (attempt_count between 0 and 1000),
  claim_started_at timestamptz,
  next_retry_at timestamptz,
  last_error_code text check (
    last_error_code is null
    or char_length(last_error_code) between 1 and 120
  ),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((object_key is null) = (storage_upload_id is null)),
  check (
    (status = 'processing' and claim_started_at is not null)
    or (status <> 'processing' and claim_started_at is null)
  ),
  check (
    (status = 'retry' and next_retry_at is not null and last_error_code is not null)
    or (status <> 'retry' and next_retry_at is null)
  ),
  check (
    (status in ('completed', 'dead_letter') and completed_at is not null)
    or (status not in ('completed', 'dead_letter') and completed_at is null)
  )
);

create table public.creator_upload_cleanup_tombstones (
  id uuid primary key default gen_random_uuid(),
  cleanup_job_id uuid not null
    references public.creator_upload_cleanup_jobs(id) on delete restrict,
  session_id uuid not null
    references public.creator_upload_sessions(id) on delete restrict,
  reason text not null check (
    reason in (
      'user_abort', 'admin_abort', 'expired',
      'rights_denied', 'verification_failed'
    )
  ),
  outcome text not null check (
    outcome in ('retry', 'completed', 'dead_letter')
  ),
  attempt_number integer not null check (attempt_number between 1 and 1000),
  multipart_aborted boolean not null,
  object_deleted boolean not null,
  ledger_deleted boolean not null,
  error_code text check (
    error_code is null or char_length(error_code) between 1 and 120
  ),
  created_at timestamptz not null default now(),
  unique (cleanup_job_id, attempt_number),
  check (
    (outcome = 'completed' and error_code is null and ledger_deleted)
    or (outcome <> 'completed' and error_code is not null)
  )
);

create index creator_upload_cleanup_due_idx
  on public.creator_upload_cleanup_jobs (next_retry_at, created_at, id)
  where status in ('queued', 'retry');
create index creator_upload_cleanup_claim_idx
  on public.creator_upload_cleanup_jobs (claim_started_at, id)
  where status = 'processing';
create index creator_upload_cleanup_tombstone_session_idx
  on public.creator_upload_cleanup_tombstones
    (session_id, created_at, attempt_number);

create or replace function public.validate_creator_upload_cleanup_job()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  upload_session public.creator_upload_sessions%rowtype;
begin
  select * into upload_session
  from public.creator_upload_sessions
  where id = new.session_id;

  if not found or upload_session.status not in ('aborted', 'expired') then
    raise exception 'cleanup job requires an aborted or expired upload session';
  end if;

  if new.reason = 'expired' and upload_session.status <> 'expired' then
    raise exception 'expiry cleanup requires an expired upload session';
  end if;

  if new.object_key is distinct from upload_session.staging_object_key
     or new.storage_upload_id is distinct from upload_session.storage_upload_id then
    raise exception 'cleanup job private storage snapshot does not match session';
  end if;

  return new;
end;
$$;

create or replace function public.prevent_creator_upload_cleanup_tombstone_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'cleanup tombstones are immutable';
end;
$$;

revoke all on function public.validate_creator_upload_cleanup_job() from public;
revoke all on function public.prevent_creator_upload_cleanup_tombstone_mutation() from public;
grant execute on function public.validate_creator_upload_cleanup_job() to service_role;
grant execute on function public.prevent_creator_upload_cleanup_tombstone_mutation() to service_role;

create trigger creator_upload_cleanup_jobs_validate
before insert or update on public.creator_upload_cleanup_jobs
for each row execute function public.validate_creator_upload_cleanup_job();

create trigger creator_upload_cleanup_jobs_updated_at
before update on public.creator_upload_cleanup_jobs
for each row execute function public.set_updated_at();

create trigger creator_upload_cleanup_tombstones_no_update
before update on public.creator_upload_cleanup_tombstones
for each row execute function public.prevent_creator_upload_cleanup_tombstone_mutation();

create trigger creator_upload_cleanup_tombstones_no_delete
before delete on public.creator_upload_cleanup_tombstones
for each row execute function public.prevent_creator_upload_cleanup_tombstone_mutation();

alter table public.creator_upload_cleanup_jobs enable row level security;
alter table public.creator_upload_cleanup_tombstones enable row level security;

revoke all on table public.creator_upload_cleanup_jobs
  from anon, authenticated, service_role;
revoke all on table public.creator_upload_cleanup_tombstones
  from anon, authenticated, service_role;
grant select, insert, update on table public.creator_upload_cleanup_jobs
  to service_role;
grant select, insert on table public.creator_upload_cleanup_tombstones
  to service_role;

create policy "service role manages creator upload cleanup jobs"
  on public.creator_upload_cleanup_jobs
  for all to service_role
  using (true)
  with check (true);

create policy "service role reads creator upload cleanup tombstones"
  on public.creator_upload_cleanup_tombstones
  for select to service_role
  using (true);
create policy "service role appends creator upload cleanup tombstones"
  on public.creator_upload_cleanup_tombstones
  for insert to service_role
  with check (true);

comment on table public.creator_upload_cleanup_jobs is
  'Private durable remote cleanup work. Never expose object_key or storage_upload_id to Nuxt.';
comment on table public.creator_upload_cleanup_tombstones is
  'Append-only audit of each creator-upload cleanup attempt; cleanup tombstones are immutable.';
comment on column public.creator_upload_cleanup_jobs.object_key is
  'Private quarantine identity; never expose to clients.';
comment on column public.creator_upload_cleanup_jobs.storage_upload_id is
  'Private MinIO multipart identity; never expose to clients.';

commit;
