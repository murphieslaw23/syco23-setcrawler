-- v0.6 Task 16 foundation: private lifecycle jobs and immutable tombstones.
-- No object promotion/deletion worker or public route is enabled here.

begin;

create table public.audio_asset_lifecycle_jobs (
  id uuid primary key default gen_random_uuid(),
  audio_asset_id uuid not null
    references public.audio_assets(id) on delete restrict,
  action text not null check (action in ('approve', 'reject', 'expire')),
  status text not null default 'queued' check (
    status in ('queued', 'claimed', 'retry', 'completed', 'failed')
  ),
  claim_token uuid,
  claim_started_at timestamptz,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  next_retry_at timestamptz,
  actor text not null check (char_length(actor) between 1 and 300),
  reason text not null check (char_length(reason) between 1 and 2000),
  last_error text check (
    last_error is null or char_length(last_error) between 1 and 2000
  ),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (
      status = 'claimed'
      and claim_token is not null
      and claim_started_at is not null
    )
    or (
      status <> 'claimed'
      and claim_token is null
      and claim_started_at is null
    )
  ),
  check (
    (status = 'completed' and completed_at is not null)
    or (status <> 'completed' and completed_at is null)
  )
);

create unique index audio_asset_lifecycle_one_active_idx
  on public.audio_asset_lifecycle_jobs (audio_asset_id)
  where status in ('queued', 'claimed', 'retry');
create index audio_asset_lifecycle_claim_idx
  on public.audio_asset_lifecycle_jobs (
    status, next_retry_at, claim_started_at, created_at, id
  );
create index audio_assets_quarantine_expiry_idx
  on public.audio_assets (expires_at, id)
  where state = 'quarantine' and expires_at is not null;

create table public.audio_asset_lifecycle_tombstones (
  id uuid primary key default gen_random_uuid(),
  lifecycle_job_id uuid not null unique
    references public.audio_asset_lifecycle_jobs(id) on delete restrict,
  audio_asset_id uuid not null
    references public.audio_assets(id) on delete restrict,
  action text not null check (action in ('approve', 'reject', 'expire')),
  actor text not null check (char_length(actor) between 1 and 300),
  reason text not null check (char_length(reason) between 1 and 2000),
  storage_outcome text not null check (
    storage_outcome in ('promoted', 'deleted')
  ),
  checksum_sha256 text not null check (
    checksum_sha256 ~ '^[0-9a-f]{64}$'
  ),
  size_bytes bigint not null check (size_bytes between 1 and 5368709120),
  before_state jsonb not null check (jsonb_typeof(before_state) = 'object'),
  after_state jsonb not null check (jsonb_typeof(after_state) = 'object'),
  created_at timestamptz not null default now(),
  check (
    (action = 'approve' and storage_outcome = 'promoted')
    or (action in ('reject', 'expire') and storage_outcome = 'deleted')
  )
);
create index audio_asset_lifecycle_tombstone_asset_idx
  on public.audio_asset_lifecycle_tombstones (
    audio_asset_id, created_at, id
  );

create or replace function public.prevent_audio_lifecycle_tombstone_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'audio lifecycle tombstones are immutable';
end
$$;

revoke all on function public.prevent_audio_lifecycle_tombstone_mutation()
  from public;
grant execute on function public.prevent_audio_lifecycle_tombstone_mutation()
  to service_role;

create trigger audio_asset_lifecycle_jobs_updated_at
before update on public.audio_asset_lifecycle_jobs
for each row execute function public.set_updated_at();

create trigger audio_asset_lifecycle_tombstones_immutable
before update or delete on public.audio_asset_lifecycle_tombstones
for each row execute function public.prevent_audio_lifecycle_tombstone_mutation();

alter table public.audio_asset_lifecycle_jobs enable row level security;
alter table public.audio_asset_lifecycle_tombstones enable row level security;

revoke all on table public.audio_asset_lifecycle_jobs
  from anon, authenticated, service_role;
revoke all on table public.audio_asset_lifecycle_tombstones
  from anon, authenticated, service_role;
grant select, insert, update on table public.audio_asset_lifecycle_jobs
  to service_role;
grant select, insert on table public.audio_asset_lifecycle_tombstones
  to service_role;

create policy "service role manages audio lifecycle jobs"
  on public.audio_asset_lifecycle_jobs
  for all to service_role
  using (true)
  with check (true);
create policy "service role appends audio lifecycle tombstones"
  on public.audio_asset_lifecycle_tombstones
  for insert to service_role
  with check (true);
create policy "service role reads audio lifecycle tombstones"
  on public.audio_asset_lifecycle_tombstones
  for select to service_role
  using (true);

comment on table public.audio_asset_lifecycle_jobs is
  'Private durable promotion, rejection, and expiry work. No object operation is enabled by this migration.';
comment on table public.audio_asset_lifecycle_tombstones is
  'Immutable audit evidence retained after private object promotion or deletion.';
comment on column public.audio_asset_lifecycle_tombstones.before_state is
  'Server-only lifecycle snapshot; never expose object keys through public DTOs.';
comment on column public.audio_asset_lifecycle_tombstones.after_state is
  'Server-only lifecycle snapshot; never expose object keys through public DTOs.';

commit;
