-- v0.6 service-role-only resumable creator upload state.
-- No route, storage writer, public URL, or runtime worker is enabled here.

begin;

create table public.creator_upload_sessions (
  id uuid primary key default gen_random_uuid(),
  audio_input_job_id uuid not null unique
    references public.audio_input_jobs(id) on delete restrict,
  rights_review_id uuid not null
    references public.rights_reviews(id) on delete restrict,
  expected_size_bytes bigint not null check (
    expected_size_bytes between 1 and 5368709120
  ),
  received_size_bytes bigint not null default 0 check (
    received_size_bytes >= 0
    and received_size_bytes <= expected_size_bytes
  ),
  content_type text not null check (
    content_type in (
      'application/ogg',
      'audio/aac',
      'audio/flac',
      'audio/mpeg',
      'audio/mp4',
      'audio/ogg',
      'audio/wav',
      'audio/x-aac',
      'audio/x-flac',
      'audio/x-wav'
    )
  ),
  expected_sha256 text check (
    expected_sha256 is null or expected_sha256 ~ '^[0-9a-f]{64}$'
  ),
  staging_object_key text unique check (
    staging_object_key is null
    or staging_object_key ~ '^objects/[0-9a-f]{2}/[0-9a-f]{32}$'
  ),
  storage_upload_id text check (
    storage_upload_id is null
    or char_length(storage_upload_id) between 1 and 2048
  ),
  status text not null default 'initiated' check (
    status in (
      'initiated', 'uploading', 'awaiting_attestation',
      'completed', 'aborted', 'expired'
    )
  ),
  attestation_evidence_id uuid
    references public.rights_evidence(id) on delete restrict,
  attested_by text check (
    attested_by is null or char_length(attested_by) between 1 and 300
  ),
  attested_at timestamptz,
  expires_at timestamptz not null,
  created_by text not null
    check (char_length(created_by) between 1 and 300),
  version integer not null default 0 check (version >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (expires_at > created_at),
  check (
    status not in ('awaiting_attestation', 'completed')
    or received_size_bytes = expected_size_bytes
  ),
  check (
    status <> 'completed'
    or (
      attestation_evidence_id is not null
      and attested_by is not null
      and attested_at is not null
    )
  ),
  check (
    status = 'completed'
    or (
      attestation_evidence_id is null
      and attested_by is null
      and attested_at is null
    )
  ),
  check (
    (
      status = 'initiated'
      and staging_object_key is null
      and storage_upload_id is null
    )
    or (
      status in ('uploading', 'awaiting_attestation', 'completed')
      and staging_object_key is not null
      and storage_upload_id is not null
    )
    or status in ('aborted', 'expired')
  )
);

create or replace function public.validate_creator_upload_session()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  input_job public.audio_input_jobs%rowtype;
  evidence public.rights_evidence%rowtype;
begin
  select * into input_job
  from public.audio_input_jobs
  where id = new.audio_input_job_id;

  if not found
     or input_job.input_kind <> 'creator_upload'
     or input_job.rights_review_id <> new.rights_review_id then
    raise exception 'creator upload session must match a creator-upload input job';
  end if;

  if new.attestation_evidence_id is not null then
    select * into evidence
    from public.rights_evidence
    where id = new.attestation_evidence_id;

    if not found
       or evidence.rights_review_id <> new.rights_review_id
       or evidence.evidence_type <> 'creator_attestation' then
      raise exception 'creator upload attestation must match its rights review';
    end if;
  end if;

  if new.status = 'completed'
     and (
       input_job.status <> 'completed'
       or input_job.audio_asset_id is null
     ) then
    raise exception 'creator upload cannot complete before its quarantine asset';
  end if;

  if tg_op = 'UPDATE'
     and old.attestation_evidence_id is not null
     and new.attestation_evidence_id is distinct from old.attestation_evidence_id then
    raise exception 'creator upload attestation is immutable';
  end if;

  return new;
end;
$$;

revoke all on function public.validate_creator_upload_session() from public;
grant execute on function public.validate_creator_upload_session() to service_role;

create trigger creator_upload_sessions_validate
before insert or update on public.creator_upload_sessions
for each row execute function public.validate_creator_upload_session();

create trigger creator_upload_sessions_updated_at
before update on public.creator_upload_sessions
for each row execute function public.set_updated_at();

create index creator_upload_sessions_expiry_idx
  on public.creator_upload_sessions (expires_at, id)
  where status in ('initiated', 'uploading', 'awaiting_attestation');
create index creator_upload_sessions_review_idx
  on public.creator_upload_sessions (rights_review_id, created_at, id);

alter table public.creator_upload_sessions enable row level security;

revoke all on table public.creator_upload_sessions
  from anon, authenticated, service_role;
grant select, insert, update on table public.creator_upload_sessions
  to service_role;

create policy "service role manages creator upload sessions"
  on public.creator_upload_sessions
  for all to service_role
  using (true)
  with check (true);

comment on table public.creator_upload_sessions is
  'Private resumable creator upload state. Never expose storage identity through public DTOs.';
comment on column public.creator_upload_sessions.staging_object_key is
  'Opaque server-generated quarantine key; never returned to the Nuxt client.';
comment on column public.creator_upload_sessions.storage_upload_id is
  'Private storage transport state; never returned to the Nuxt client.';

commit;
