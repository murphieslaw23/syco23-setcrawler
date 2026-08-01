-- v0.6 rights-policy foundation. No acquisition or object write path is enabled.

begin;

create table public.rights_reviews (
  id uuid primary key default gen_random_uuid(),
  set_id uuid not null references public.sets(id) on delete restrict,
  provider_id uuid not null references public.providers(id) on delete restrict,
  provider_external_id text not null
    check (char_length(provider_external_id) between 1 and 512),
  requested_stream boolean not null,
  requested_download boolean not null,
  allow_stream boolean not null default false,
  allow_download boolean not null default false,
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected', 'expired')),
  submitted_by text not null
    check (char_length(submitted_by) between 1 and 300),
  decided_by text check (
    decided_by is null or char_length(decided_by) between 1 and 300
  ),
  decision_reason text check (
    decision_reason is null or char_length(decision_reason) between 1 and 2000
  ),
  decided_at timestamptz,
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (requested_stream or requested_download),
  check (not allow_stream or requested_stream),
  check (not allow_download or requested_download),
  check (status = 'approved' or (not allow_stream and not allow_download)),
  check (
    (status = 'pending' and decided_by is null and decided_at is null)
    or (status <> 'pending' and decided_by is not null and decided_at is not null)
  )
);

create unique index rights_reviews_one_pending_request_idx
  on public.rights_reviews (
    set_id, provider_id, provider_external_id,
    requested_stream, requested_download
  ) where status = 'pending';
create index rights_reviews_queue_idx
  on public.rights_reviews (status, created_at, id);

create table public.rights_evidence (
  id uuid primary key default gen_random_uuid(),
  rights_review_id uuid not null
    references public.rights_reviews(id) on delete restrict,
  evidence_type text not null check (
    evidence_type in (
      'creator_attestation',
      'provider_permission',
      'permissive_license',
      'contract'
    )
  ),
  reference_url text not null check (
    char_length(reference_url) between 8 and 4096
    and reference_url ~ '^https://'
  ),
  assertions jsonb not null default '{}'::jsonb
    check (jsonb_typeof(assertions) = 'object'),
  submitted_by text not null
    check (char_length(submitted_by) between 1 and 300),
  created_at timestamptz not null default now()
);
create index rights_evidence_review_idx
  on public.rights_evidence (rights_review_id, created_at, id);

create table public.audio_permissions (
  id uuid primary key default gen_random_uuid(),
  rights_review_id uuid not null unique
    references public.rights_reviews(id) on delete restrict,
  allow_stream boolean not null,
  allow_download boolean not null,
  approved_by text not null
    check (char_length(approved_by) between 1 and 300),
  approved_at timestamptz not null default now(),
  revoked_at timestamptz,
  check (allow_stream or allow_download)
);

create table public.audio_assets (
  id uuid primary key default gen_random_uuid(),
  rights_review_id uuid not null
    references public.rights_reviews(id) on delete restrict,
  bucket_name text not null check (
    bucket_name in ('audio-quarantine', 'audio-originals')
  ),
  object_key text not null,
  checksum_sha256 text not null check (checksum_sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint not null check (size_bytes between 1 and 5368709120),
  state text not null default 'quarantine' check (
    state in ('quarantine', 'approved', 'rejected', 'expired')
  ),
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (object_key)
);

create table public.audio_versions (
  id uuid primary key default gen_random_uuid(),
  audio_asset_id uuid not null
    references public.audio_assets(id) on delete restrict,
  version_type text not null check (version_type in ('original', 'derivative')),
  bucket_name text not null check (
    bucket_name in ('audio-originals', 'audio-derivatives')
  ),
  object_key text not null,
  checksum_sha256 text not null check (checksum_sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint not null check (size_bytes between 1 and 5368709120),
  mime_type text not null check (char_length(mime_type) between 3 and 100),
  created_at timestamptz not null default now(),
  unique (object_key),
  unique (audio_asset_id, version_type, checksum_sha256)
);

create table public.rights_review_events (
  id uuid primary key default gen_random_uuid(),
  rights_review_id uuid not null
    references public.rights_reviews(id) on delete restrict,
  action text not null check (action in ('approve', 'reject', 'expire')),
  actor text not null check (char_length(actor) between 1 and 300),
  reason text not null check (char_length(reason) between 1 and 2000),
  before_state jsonb not null check (jsonb_typeof(before_state) = 'object'),
  after_state jsonb not null check (jsonb_typeof(after_state) = 'object'),
  created_at timestamptz not null default now()
);
create index rights_review_events_review_idx
  on public.rights_review_events (rights_review_id, created_at, id);

create or replace function public.prevent_rights_event_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'rights review events are immutable';
end
$$;

create trigger rights_review_events_immutable
before update or delete on public.rights_review_events
for each row execute function public.prevent_rights_event_mutation();

create trigger rights_reviews_updated_at
before update on public.rights_reviews
for each row execute function public.set_updated_at();
create trigger audio_assets_updated_at
before update on public.audio_assets
for each row execute function public.set_updated_at();

alter table public.rights_reviews enable row level security;
alter table public.rights_evidence enable row level security;
alter table public.audio_permissions enable row level security;
alter table public.audio_assets enable row level security;
alter table public.audio_versions enable row level security;
alter table public.rights_review_events enable row level security;

revoke all on table public.rights_reviews from anon, authenticated, service_role;
revoke all on table public.rights_evidence from anon, authenticated, service_role;
revoke all on table public.audio_permissions from anon, authenticated, service_role;
revoke all on table public.audio_assets from anon, authenticated, service_role;
revoke all on table public.audio_versions from anon, authenticated, service_role;
revoke all on table public.rights_review_events from anon, authenticated, service_role;

grant select, insert, update on table public.rights_reviews to authenticated;
grant select, insert on table public.rights_evidence to authenticated;
grant select on table public.rights_review_events to authenticated;
grant select, insert, update on table public.rights_reviews to service_role;
grant select, insert on table public.rights_evidence to service_role;
grant select, insert on table public.rights_review_events to service_role;
grant select, insert, update on table public.audio_permissions to service_role;
grant select, insert, update on table public.audio_assets to service_role;
grant select, insert on table public.audio_versions to service_role;

create policy "admins manage rights reviews" on public.rights_reviews
  for all to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));
create policy "admins append rights evidence" on public.rights_evidence
  for insert to authenticated
  with check (private.has_role('admin'));
create policy "admins read rights evidence" on public.rights_evidence
  for select to authenticated
  using (private.has_role('admin'));
create policy "admins read rights events" on public.rights_review_events
  for select to authenticated
  using (private.has_role('admin'));

comment on table public.rights_reviews is
  'Metadata-only rights decisions; approval does not acquire or publish audio.';
comment on table public.audio_assets is
  'Private server-side asset identities; no object writer is enabled in this slice.';
comment on column public.audio_assets.object_key is
  'Opaque server-generated identity that must never reach client responses.';

commit;
