alter table public.search_profiles
  add column if not exists operation text not null default 'search',
  add column if not exists parameters jsonb not null default '{}'::jsonb,
  add column if not exists last_scheduled_at timestamptz,
  add column if not exists next_scheduled_at timestamptz;

alter table public.search_profiles
  add constraint search_profiles_source_provider_key
    check (source ~ '^[a-z][a-z0-9-]{0,63}$'),
  add constraint search_profiles_operation_name
    check (operation ~ '^[a-z][a-z0-9_-]{0,79}$');

update public.search_profiles
set parameters = jsonb_build_object('query', query)
where parameters = '{}'::jsonb;
