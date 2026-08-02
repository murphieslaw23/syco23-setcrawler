-- v0.6 durable rights-gated audio input jobs. No public object route is enabled.

begin;

alter table public.audio_assets
  add column content_type text check (
    content_type is null or char_length(content_type) between 3 and 100
  );

alter table public.audio_assets
  add constraint audio_assets_opaque_object_key_check
  check (object_key ~ '^objects/[0-9a-f]{2}/[0-9a-f]{32}$'),
  add constraint audio_assets_bucket_state_check
  check (
    (state = 'approved' and bucket_name = 'audio-originals')
    or (
      state in ('quarantine', 'rejected', 'expired')
      and bucket_name = 'audio-quarantine'
    )
  ),
  add constraint audio_assets_quarantine_expiry_check
  check (state <> 'quarantine' or expires_at is not null);

alter table public.audio_versions
  add constraint audio_versions_opaque_object_key_check
  check (object_key ~ '^objects/[0-9a-f]{2}/[0-9a-f]{32}$');

create table public.audio_input_jobs (
  id uuid primary key default gen_random_uuid(),
  rights_review_id uuid not null
    references public.rights_reviews(id) on delete restrict,
  provider_id uuid references public.providers(id) on delete restrict,
  provider_item_external_id text check (
    provider_item_external_id is null
    or char_length(provider_item_external_id) between 1 and 512
  ),
  candidate_external_id text not null
    check (char_length(candidate_external_id) between 1 and 512),
  input_kind text not null check (
    input_kind in ('provider_acquisition', 'creator_upload')
  ),
  source_url text check (
    source_url is null
    or (
      char_length(source_url) between 8 and 4096
      and source_url ~ '^https://'
    )
  ),
  expected_sha256 text check (
    expected_sha256 is null or expected_sha256 ~ '^[0-9a-f]{64}$'
  ),
  status text not null default 'queued' check (
    status in (
      'queued', 'processing', 'retry', 'completed',
      'failed', 'blocked', 'dead_letter'
    )
  ),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  claim_started_at timestamptz,
  next_retry_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  audio_asset_id uuid unique
    references public.audio_assets(id) on delete restrict,
  created_by text not null
    check (char_length(created_by) between 1 and 300),
  details jsonb not null default '{}'::jsonb
    check (jsonb_typeof(details) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (
      input_kind = 'provider_acquisition'
      and provider_id is not null
      and provider_item_external_id is not null
      and source_url is not null
    )
    or (
      input_kind = 'creator_upload'
      and provider_id is null
      and provider_item_external_id is null
      and source_url is null
    )
  ),
  check (
    (status = 'processing' and claim_started_at is not null and started_at is not null)
    or status <> 'processing'
  ),
  check (
    (
      status = 'completed'
      and audio_asset_id is not null
      and finished_at is not null
    )
    or (
      status <> 'completed'
      and audio_asset_id is null
    )
  )
);

create unique index audio_input_jobs_one_active_candidate_idx
  on public.audio_input_jobs (
    rights_review_id, input_kind, candidate_external_id
  ) where status in ('queued', 'processing', 'retry');

create index audio_input_jobs_claim_idx
  on public.audio_input_jobs (
    status, next_retry_at, claim_started_at, created_at, id
  );
create index audio_input_jobs_review_idx
  on public.audio_input_jobs (rights_review_id, created_at, id);
create index audio_input_jobs_asset_idx
  on public.audio_input_jobs (audio_asset_id)
  where audio_asset_id is not null;

create trigger audio_input_jobs_updated_at
before update on public.audio_input_jobs
for each row execute function public.set_updated_at();

alter table public.audio_input_jobs enable row level security;

revoke all on table public.audio_input_jobs from anon, authenticated, service_role;
grant select, insert, update on table public.audio_input_jobs to service_role;

create policy "service role manages audio input jobs"
  on public.audio_input_jobs
  for all to service_role
  using (true)
  with check (true);

comment on table public.audio_input_jobs is
  'Private durable provider-acquisition and creator-upload jobs bound to approved rights reviews.';
comment on column public.audio_input_jobs.source_url is
  'Server-side provider URL. Never return this column through public DTOs.';
comment on column public.audio_input_jobs.audio_asset_id is
  'Set only atomically when a private audio-quarantine asset is persisted.';
comment on column public.audio_assets.object_key is
  'Opaque server-generated key matching ^objects/[0-9a-f]{2}/[0-9a-f]{32}$.';
comment on column public.audio_assets.bucket_name is
  'Quarantine remains private in audio-quarantine; approved assets move to audio-originals.';

commit;
