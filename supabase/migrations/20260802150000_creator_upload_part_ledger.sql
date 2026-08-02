-- v0.6 service-role-only multipart manifest and part ledger.
-- Transport state remains private and is never returned to Nuxt.

begin;

create table public.creator_upload_manifests (
  session_id uuid primary key
    references public.creator_upload_sessions(id) on delete restrict,
  part_size_bytes bigint not null check (
    part_size_bytes between 5242880 and 5368709120
  ),
  expected_part_count integer not null check (
    expected_part_count between 1 and 10000
  ),
  created_at timestamptz not null default now()
);

create table public.creator_upload_parts (
  session_id uuid not null
    references public.creator_upload_manifests(session_id) on delete restrict,
  part_number integer not null check (part_number between 1 and 10000),
  etag text not null check (
    char_length(etag) between 1 and 512
    and etag = btrim(etag)
  ),
  size_bytes bigint not null check (
    size_bytes between 1 and 5368709120
  ),
  checksum_sha256 text not null check (
    checksum_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at timestamptz not null default now(),
  primary key (session_id, part_number)
);

create index creator_upload_parts_session_created_idx
  on public.creator_upload_parts (session_id, created_at, part_number);

create or replace function public.validate_creator_upload_manifest()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  upload_session public.creator_upload_sessions%rowtype;
  calculated_part_count bigint;
begin
  select * into upload_session
  from public.creator_upload_sessions
  where id = new.session_id;

  if not found
     or upload_session.status <> 'initiated'
     or upload_session.staging_object_key is not null
     or upload_session.storage_upload_id is not null then
    raise exception 'multipart manifest requires an initiated private upload session';
  end if;

  if upload_session.expires_at <= now() then
    raise exception 'multipart manifest cannot attach to an expired upload session';
  end if;

  calculated_part_count :=
    (upload_session.expected_size_bytes + new.part_size_bytes - 1)
    / new.part_size_bytes;

  if calculated_part_count <> new.expected_part_count then
    raise exception 'multipart manifest does not match the declared upload size';
  end if;

  return new;
end;
$$;

create or replace function public.validate_creator_upload_part()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  upload_session public.creator_upload_sessions%rowtype;
  manifest public.creator_upload_manifests%rowtype;
  expected_size bigint;
begin
  select * into upload_session
  from public.creator_upload_sessions
  where id = new.session_id;

  select * into manifest
  from public.creator_upload_manifests
  where session_id = new.session_id;

  if not found or upload_session.id is null then
    raise exception 'multipart part requires a private upload manifest';
  end if;

  if upload_session.status <> 'uploading' then
    raise exception 'multipart parts require an uploading session';
  end if;

  if upload_session.expires_at <= now() then
    raise exception 'multipart part cannot attach to an expired upload session';
  end if;

  if new.part_number > manifest.expected_part_count then
    raise exception 'multipart part number is outside the upload plan';
  end if;

  expected_size := least(
    manifest.part_size_bytes,
    upload_session.expected_size_bytes
      - ((new.part_number - 1)::bigint * manifest.part_size_bytes)
  );

  if expected_size < 1 or new.size_bytes <> expected_size then
    raise exception 'multipart part size does not match the upload plan';
  end if;

  return new;
end;
$$;

create or replace function public.prevent_creator_upload_ledger_update()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'creator upload multipart ledger rows are immutable';
end;
$$;

create or replace function public.guard_creator_upload_ledger_delete()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  upload_status text;
begin
  select status into upload_status
  from public.creator_upload_sessions
  where id = old.session_id;

  if upload_status not in ('aborted', 'expired') then
    raise exception 'multipart ledger cleanup requires an aborted or expired session';
  end if;

  return old;
end;
$$;

revoke all on function public.validate_creator_upload_manifest() from public;
revoke all on function public.validate_creator_upload_part() from public;
revoke all on function public.prevent_creator_upload_ledger_update() from public;
revoke all on function public.guard_creator_upload_ledger_delete() from public;
grant execute on function public.validate_creator_upload_manifest() to service_role;
grant execute on function public.validate_creator_upload_part() to service_role;
grant execute on function public.prevent_creator_upload_ledger_update() to service_role;
grant execute on function public.guard_creator_upload_ledger_delete() to service_role;

create trigger creator_upload_manifests_validate
before insert on public.creator_upload_manifests
for each row execute function public.validate_creator_upload_manifest();

create trigger creator_upload_parts_validate
before insert on public.creator_upload_parts
for each row execute function public.validate_creator_upload_part();

create trigger creator_upload_manifests_immutable
before update on public.creator_upload_manifests
for each row execute function public.prevent_creator_upload_ledger_update();

create trigger creator_upload_parts_immutable
before update on public.creator_upload_parts
for each row execute function public.prevent_creator_upload_ledger_update();

create trigger creator_upload_manifests_delete_guard
before delete on public.creator_upload_manifests
for each row execute function public.guard_creator_upload_ledger_delete();

create trigger creator_upload_parts_delete_guard
before delete on public.creator_upload_parts
for each row execute function public.guard_creator_upload_ledger_delete();

alter table public.creator_upload_manifests enable row level security;
alter table public.creator_upload_parts enable row level security;

revoke all on table public.creator_upload_manifests
  from anon, authenticated, service_role;
revoke all on table public.creator_upload_parts
  from anon, authenticated, service_role;
grant select, insert, delete on table public.creator_upload_manifests
  to service_role;
grant select, insert, delete on table public.creator_upload_parts
  to service_role;

create policy "service role manages creator upload manifests"
  on public.creator_upload_manifests
  for all to service_role
  using (true)
  with check (true);

create policy "service role manages creator upload parts"
  on public.creator_upload_parts
  for all to service_role
  using (true)
  with check (true);

comment on table public.creator_upload_manifests is
  'Private multipart plan bound to one creator upload session; never expose to Nuxt.';
comment on table public.creator_upload_parts is
  'Private immutable multipart ETag, size, and checksum ledger; never expose to Nuxt.';
comment on column public.creator_upload_parts.etag is
  'Private MinIO transport identity; never returned to a client.';

commit;
