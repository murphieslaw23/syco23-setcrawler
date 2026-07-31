-- v0.3 provider identity and canonical source links.
-- This migration is additive: public.sets source/source_id remain unchanged.

begin;

create or replace function public.provider_capabilities_valid(value text[])
returns boolean
language sql
immutable
strict
set search_path = pg_catalog
as $$
  select
    cardinality(value) > 0
    and array_position(value, null) is null
    and value <@ array[
      'discovery',
      'metadata',
      'embed',
      'authorized_audio',
      'creator_upload',
      'syndication',
      'license_evidence'
    ]::text[]
    and cardinality(value) = (
      select count(distinct capability)
      from unnest(value) as capability
    )
$$;

create table public.providers (
  id uuid primary key default gen_random_uuid(),
  key text not null unique
    check (
      char_length(key) between 1 and 64
      and key ~ '^[a-z][a-z0-9-]*$'
    ),
  display_name text not null
    check (
      char_length(display_name) between 1 and 128
      and display_name = btrim(display_name)
    ),
  capabilities text[] not null
    check (public.provider_capabilities_valid(capabilities)),
  enabled boolean not null,
  workload_policy jsonb not null
    check (jsonb_typeof(workload_policy) = 'object'),
  descriptor_version integer not null
    check (descriptor_version > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.provider_items (
  id uuid primary key default gen_random_uuid(),
  provider_id uuid not null
    references public.providers(id) on delete restrict,
  external_id text not null
    check (
      char_length(external_id) between 1 and 512
      and external_id = btrim(external_id)
    ),
  canonical_url text not null
    check (
      char_length(canonical_url) between 8 and 4096
      and canonical_url ~ '^https?://'
    ),
  item_type text not null default 'set_candidate'
    check (item_type = 'set_candidate'),
  title text check (title is null or char_length(title) <= 500),
  published_at timestamptz,
  duration_seconds integer
    check (duration_seconds is null or duration_seconds >= 0),
  embed_url text
    check (
      embed_url is null
      or (
        char_length(embed_url) between 8 and 4096
        and embed_url ~ '^https?://'
      )
    ),
  raw_metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(raw_metadata) = 'object'),
  metadata_fetched_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (provider_id, external_id)
);

create table public.set_provider_items (
  set_id uuid not null
    references public.sets(id) on delete cascade,
  provider_item_id uuid not null
    references public.provider_items(id) on delete cascade,
  relationship text not null default 'source'
    check (relationship = 'source'),
  is_primary boolean not null default false,
  created_at timestamptz not null default now(),
  unique (set_id, provider_item_id, relationship)
);

create unique index set_provider_items_one_primary_source_idx
  on public.set_provider_items (set_id)
  where relationship = 'source' and is_primary;

create index provider_items_canonical_url_idx
  on public.provider_items (canonical_url);
create index provider_items_provider_created_idx
  on public.provider_items (provider_id, created_at desc);
create index set_provider_items_provider_item_idx
  on public.set_provider_items (provider_item_id);
create index set_provider_items_set_relationship_idx
  on public.set_provider_items (set_id, relationship);

create trigger providers_updated_at
before update on public.providers
for each row execute function public.set_updated_at();

create trigger provider_items_updated_at
before update on public.provider_items
for each row execute function public.set_updated_at();

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
    '00000000-0000-4000-8000-000000030001',
    'youtube',
    'YouTube',
    array['discovery', 'metadata', 'embed'],
    true,
    '{
      "discovery":"provider-api",
      "metadata":"provider-api",
      "embed":"provider-api"
    }'::jsonb,
    1
  ),
  (
    '00000000-0000-4000-8000-000000030002',
    'soundcloud',
    'SoundCloud',
    array['metadata', 'embed'],
    true,
    '{
      "metadata":"provider-scrape",
      "embed":"provider-scrape"
    }'::jsonb,
    1
  ),
  (
    '00000000-0000-4000-8000-000000030003',
    'ftm',
    'freeteknomusic.org',
    array['discovery', 'metadata', 'license_evidence'],
    false,
    '{
      "discovery":"provider-scrape",
      "metadata":"provider-scrape",
      "license_evidence":"provider-scrape"
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

create temporary table legacy_provider_keys (
  legacy_source text primary key,
  provider_key text not null unique
) on commit drop;

insert into legacy_provider_keys (legacy_source, provider_key)
values
  ('youtube', 'youtube'),
  ('soundcloud', 'soundcloud'),
  ('freeteknomusic', 'ftm');

do $$
declare
  unknown_source text;
begin
  select sets.source
  into unknown_source
  from public.sets as sets
  left join legacy_provider_keys as mapping
    on mapping.legacy_source = sets.source
  where mapping.provider_key is null
  order by sets.source
  limit 1;

  if unknown_source is not null then
    raise exception 'unknown legacy set source: %', unknown_source;
  end if;
end
$$;

insert into public.provider_items (
  provider_id,
  external_id,
  canonical_url,
  title,
  published_at,
  duration_seconds,
  raw_metadata,
  metadata_fetched_at,
  created_at,
  updated_at
)
select
  providers.id,
  sets.source_id,
  sets.canonical_url,
  sets.title,
  sets.published_at,
  sets.duration_seconds,
  jsonb_build_object('backfilled_from_legacy', true),
  sets.updated_at,
  sets.created_at,
  sets.updated_at
from public.sets as sets
join legacy_provider_keys as mapping
  on mapping.legacy_source = sets.source
join public.providers as providers
  on providers.key = mapping.provider_key
on conflict (provider_id, external_id) do update
set
  canonical_url = excluded.canonical_url,
  title = excluded.title,
  published_at = excluded.published_at,
  duration_seconds = excluded.duration_seconds,
  metadata_fetched_at = excluded.metadata_fetched_at,
  updated_at = excluded.updated_at;

insert into public.set_provider_items (
  set_id,
  provider_item_id,
  relationship,
  is_primary,
  created_at
)
select
  sets.id,
  provider_items.id,
  'source',
  true,
  sets.created_at
from public.sets as sets
join legacy_provider_keys as mapping
  on mapping.legacy_source = sets.source
join public.providers as providers
  on providers.key = mapping.provider_key
join public.provider_items as provider_items
  on provider_items.provider_id = providers.id
 and provider_items.external_id = sets.source_id
on conflict (set_id, provider_item_id, relationship) do update
set is_primary = true;

do $$
begin
  if exists (
    select 1
    from public.sets as sets
    left join public.set_provider_items as links
      on links.set_id = sets.id
     and links.relationship = 'source'
     and links.is_primary
    group by sets.id
    having count(links.provider_item_id) <> 1
  ) then
    raise exception 'every set must have exactly one primary source link';
  end if;

  if exists (
    select 1
    from public.sets as sets
    join legacy_provider_keys as mapping
      on mapping.legacy_source = sets.source
    join public.set_provider_items as links
      on links.set_id = sets.id
     and links.relationship = 'source'
     and links.is_primary
    join public.provider_items as provider_items
      on provider_items.id = links.provider_item_id
    join public.providers as providers
      on providers.id = provider_items.provider_id
    where providers.key <> mapping.provider_key
       or provider_items.external_id <> sets.source_id
  ) then
    raise exception 'legacy source projection mismatch';
  end if;
end
$$;

alter table public.providers enable row level security;
alter table public.provider_items enable row level security;
alter table public.set_provider_items enable row level security;

revoke all on table public.providers from anon, authenticated, service_role;
revoke all on table public.provider_items from anon, authenticated, service_role;
revoke all on table public.set_provider_items from anon, authenticated, service_role;

grant select, insert, update, delete
  on table public.providers,
    public.provider_items,
    public.set_provider_items
  to authenticated;

grant select, insert, update, delete
  on table public.providers,
    public.provider_items,
    public.set_provider_items
  to service_role;

create policy "editors read providers" on public.providers
  for select to authenticated
  using (private.has_role('editor') or private.has_role('admin'));
create policy "admins manage providers" on public.providers
  for all to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));

create policy "editors read provider items" on public.provider_items
  for select to authenticated
  using (private.has_role('editor') or private.has_role('admin'));
create policy "admins manage provider items" on public.provider_items
  for all to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));

create policy "editors read set provider items" on public.set_provider_items
  for select to authenticated
  using (private.has_role('editor') or private.has_role('admin'));
create policy "admins manage set provider items" on public.set_provider_items
  for all to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));

comment on table public.providers is
  'Operational provider identities synchronized from application descriptors.';
comment on table public.provider_items is
  'Metadata-only identities of provider-native set candidates.';
comment on table public.set_provider_items is
  'Canonical set to provider-item relationships; v0.3 backfills one primary source.';
comment on column public.provider_items.raw_metadata is
  'Sanitized server-only provider metadata; never expose wholesale publicly.';

commit;
