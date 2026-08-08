-- v0.7 Task 17: durable private audio processing ledger and version metadata.
-- This migration creates no public audio access and performs no media processing.

begin;

alter table public.audio_assets
  drop constraint if exists audio_assets_state_check;
alter table public.audio_assets
  add constraint audio_assets_state_check check (
    state in (
      'quarantine', 'approved', 'rejected', 'expired',
      'processing', 'ready', 'failed'
    )
  );

alter table public.audio_versions
  add column if not exists codec_name text
    check (codec_name is null or char_length(codec_name) between 1 and 64),
  add column if not exists format_name text
    check (format_name is null or char_length(format_name) between 1 and 128),
  add column if not exists duration_seconds double precision
    check (duration_seconds is null or duration_seconds > 0),
  add column if not exists bit_rate integer
    check (bit_rate is null or bit_rate > 0),
  add column if not exists sample_rate integer
    check (sample_rate is null or sample_rate > 0),
  add column if not exists channels integer
    check (channels is null or channels between 1 and 32),
  add column if not exists metadata_tags jsonb
    check (metadata_tags is null or jsonb_typeof(metadata_tags) = 'object');

create table public.audio_processing_jobs (
  id uuid primary key default gen_random_uuid(),
  audio_asset_id uuid not null unique
    references public.audio_assets(id) on delete restrict,
  status text not null default 'queued'
    check (status in ('queued', 'claimed', 'retry', 'completed', 'failed')),
  claim_token uuid,
  claim_started_at timestamptz,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  derivative_object_key text check (
    derivative_object_key is null
    or char_length(derivative_object_key) between 1 and 512
  ),
  next_retry_at timestamptz,
  last_error text check (
    last_error is null or char_length(last_error) between 1 and 2000
  ),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (status = 'claimed' and claim_token is not null and claim_started_at is not null)
    or (status <> 'claimed' and claim_token is null and claim_started_at is null)
  ),
  check (
    (status = 'retry' and next_retry_at is not null)
    or (status <> 'retry' and next_retry_at is null)
  ),
  check (
    (status = 'completed' and completed_at is not null)
    or (status <> 'completed' and completed_at is null)
  )
);

create index audio_processing_jobs_due_idx
  on public.audio_processing_jobs (status, next_retry_at, created_at, id);
create index audio_processing_jobs_claim_idx
  on public.audio_processing_jobs (status, claim_started_at, id);

create trigger audio_processing_jobs_updated_at
before update on public.audio_processing_jobs
for each row execute function public.set_updated_at();

create or replace function public.enqueue_audio_processing_after_approval()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if old.state is not distinct from new.state then
    return new;
  end if;

  if new.state = 'approved' and new.bucket_name = 'audio-originals' then
    insert into public.audio_processing_jobs (audio_asset_id)
    values (new.id)
    on conflict (audio_asset_id) do nothing;
  end if;

  return new;
end
$$;

revoke all on function public.enqueue_audio_processing_after_approval()
  from public;
grant execute on function public.enqueue_audio_processing_after_approval()
  to authenticated, service_role;

create trigger audio_assets_enqueue_processing
after update of state on public.audio_assets
for each row execute function public.enqueue_audio_processing_after_approval();

insert into public.audio_processing_jobs (audio_asset_id)
select assets.id
from public.audio_assets as assets
where assets.state = 'approved'
  and assets.bucket_name = 'audio-originals'
on conflict (audio_asset_id) do nothing;

alter table public.audio_processing_jobs enable row level security;
revoke all on table public.audio_processing_jobs
  from anon, authenticated, service_role;
grant select, insert, update on table public.audio_processing_jobs
  to service_role;

comment on table public.audio_processing_jobs is
  'Private durable media-processing ledger. Jobs never grant public audio access.';
comment on column public.audio_processing_jobs.derivative_object_key is
  'Optional durable opaque destination key reserved before any derivative upload.';
comment on column public.audio_versions.metadata_tags is
  'Selected verified media tags captured after probing stored audio.';

commit;
