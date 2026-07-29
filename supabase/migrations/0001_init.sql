create extension if not exists pgcrypto;

-- Plain Postgres compatibility for local Compose. Supabase owns auth.users and
-- intentionally denies application migrations DDL access to the auth schema.
do $$
begin
  if to_regclass('auth.users') is null then
    create schema if not exists auth;
    create table auth.users (
      id uuid primary key default gen_random_uuid()
    );
  end if;
end
$$;

create table images (
  id uuid primary key default gen_random_uuid(),
  remote_url text,
  storage_path text,
  web_variant_path text,
  kind text not null check (kind in ('flyer','artist','crew','label','thumbnail')),
  width integer,
  height integer,
  perceptual_hash text,
  attribution text,
  created_at timestamptz not null default now()
);

create table sets (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('youtube','soundcloud','freeteknomusic')),
  source_id text not null,
  canonical_url text not null,
  title text not null,
  description text,
  duration_seconds integer,
  published_at timestamptz,
  set_score numeric(4,3),
  review_status text not null default 'inbox'
    check (review_status in ('inbox','reviewing','accepted','rejected','published')),
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source, source_id)
);

create table artists (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  aliases jsonb default '[]'::jsonb,
  image_id uuid references images(id),
  created_at timestamptz not null default now()
);

create table events (
  id uuid primary key default gen_random_uuid(),
  name text,
  starts_on date,
  venue text,
  city text,
  country text,
  flyer_image_id uuid references images(id),
  created_at timestamptz not null default now()
);

create table crews (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  image_id uuid references images(id),
  created_at timestamptz not null default now()
);

create table set_artists (
  set_id uuid references sets(id) on delete cascade,
  artist_id uuid references artists(id) on delete cascade,
  role text default 'primary',
  primary key (set_id, artist_id)
);

create table set_events (
  set_id uuid references sets(id) on delete cascade,
  event_id uuid references events(id) on delete cascade,
  primary key (set_id, event_id)
);

create table set_crews (
  set_id uuid references sets(id) on delete cascade,
  crew_id uuid references crews(id) on delete cascade,
  primary key (set_id, crew_id)
);

create table set_images (
  set_id uuid references sets(id) on delete cascade,
  image_id uuid references images(id) on delete cascade,
  is_primary boolean not null default false,
  priority integer default 0,
  primary key (set_id, image_id)
);

create table field_candidates (
  id uuid primary key default gen_random_uuid(),
  set_id uuid not null references sets(id) on delete cascade,
  field_name text not null,
  candidate_value text,
  confidence numeric(4,3),
  source text not null,
  accepted boolean,
  created_at timestamptz not null default now()
);

create table import_log (
  id uuid primary key default gen_random_uuid(),
  set_id uuid references sets(id) on delete cascade,
  action text not null,
  actor text,
  details jsonb,
  created_at timestamptz not null default now()
);

create table search_profiles (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  query text not null,
  source text not null default 'youtube',
  schedule_cron text default '0 6 * * *',
  last_run_at timestamptz,
  next_page_token text,
  enabled boolean default true,
  deleted_at timestamptz,
  created_at timestamptz not null default now()
);

-- Required by the role model referenced in the approved RLS contract.
create table user_roles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null check (role in ('admin','editor','viewer')),
  created_at timestamptz not null default now()
);

-- Justified by §5: thresholds and keyword lists must be editable without code changes.
create table heuristic_config (
  id text primary key default 'active' check (id = 'active'),
  minimum_duration_seconds integer not null default 1200,
  review_threshold numeric(4,3) not null default 0.400,
  high_confidence_threshold numeric(4,3) not null default 0.700,
  strong_keywords jsonb not null default '["liveset","live set","dj set","b2b","teknival","free party","freetekno","mix"]'::jsonb,
  medium_keywords jsonb not null default '["hardtek","tekno","tribe","acid","industrial","breakcore"]'::jsonb,
  negative_keywords jsonb not null default '["official video","music video","single","EP","album","tutorial","review"]'::jsonb,
  updated_at timestamptz not null default now()
);

insert into heuristic_config (id) values ('active') on conflict do nothing;

create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger sets_updated_at
before update on sets
for each row execute function set_updated_at();
