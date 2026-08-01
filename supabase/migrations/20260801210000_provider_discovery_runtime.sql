-- v0.4 provider discovery persistence. Candidates remain reference-only JSON.

begin;

alter table public.provider_items
  add column if not exists creator_name text
    check (creator_name is null or char_length(creator_name) <= 300),
  add column if not exists artwork_candidates jsonb not null default '[]'::jsonb
    check (jsonb_typeof(artwork_candidates) = 'array'),
  add column if not exists download_candidates jsonb not null default '[]'::jsonb
    check (jsonb_typeof(download_candidates) = 'array'),
  add column if not exists provenance jsonb not null default '{}'::jsonb
    check (jsonb_typeof(provenance) = 'object'),
  add column if not exists license_evidence jsonb
    check (
      license_evidence is null
      or jsonb_typeof(license_evidence) = 'object'
    );

insert into public.providers (
  id,
  key,
  display_name,
  capabilities,
  enabled,
  workload_policy,
  descriptor_version
)
values
  (
    '00000000-0000-4000-8000-000000040001',
    'archive-org',
    'Internet Archive',
    array['discovery', 'metadata', 'embed', 'license_evidence'],
    false,
    '{
      "discovery":"provider-api",
      "metadata":"provider-api",
      "embed":"provider-api",
      "license_evidence":"provider-api"
    }'::jsonb,
    1
  ),
  (
    '00000000-0000-4000-8000-000000040002',
    'mixcloud',
    'Mixcloud',
    array['discovery', 'metadata', 'embed', 'syndication'],
    false,
    '{
      "discovery":"provider-api",
      "metadata":"provider-api",
      "embed":"provider-api",
      "syndication":"provider-api"
    }'::jsonb,
    1
  ),
  (
    '00000000-0000-4000-8000-000000040003',
    'audius',
    'Audius',
    array['discovery', 'metadata', 'embed', 'license_evidence'],
    false,
    '{
      "discovery":"provider-api",
      "metadata":"provider-api",
      "embed":"provider-api",
      "license_evidence":"provider-api"
    }'::jsonb,
    1
  ),
  (
    '00000000-0000-4000-8000-000000040004',
    'rss',
    'RSS / Atom',
    array['discovery', 'syndication'],
    false,
    '{
      "discovery":"provider-api",
      "syndication":"provider-api"
    }'::jsonb,
    1
  )
on conflict (key) do update
set
  display_name = excluded.display_name,
  capabilities = excluded.capabilities,
  workload_policy = excluded.workload_policy,
  descriptor_version = excluded.descriptor_version,
  updated_at = now();

commit;
