-- v0.6 Task 16: atomically hand rights decisions to private lifecycle work.
-- The trigger only creates durable database jobs; it never touches object storage.

begin;

create or replace function public.enqueue_rights_lifecycle_handoff()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  lifecycle_action text;
begin
  if old.status is not distinct from new.status then
    return new;
  end if;

  if new.status not in ('approved', 'rejected') then
    return new;
  end if;

  lifecycle_action := case
    when new.status = 'approved' then 'approve'
    else 'reject'
  end;

  insert into public.audio_asset_lifecycle_jobs (
    audio_asset_id,
    action,
    actor,
    reason
  )
  select
    assets.id,
    lifecycle_action,
    new.decided_by,
    new.decision_reason
  from public.audio_assets as assets
  where assets.rights_review_id = new.id
    and assets.state = 'quarantine'
    and assets.bucket_name = 'audio-quarantine'
  order by assets.created_at, assets.id;

  return new;
end
$$;

revoke all on function public.enqueue_rights_lifecycle_handoff()
  from public;
grant execute on function public.enqueue_rights_lifecycle_handoff()
  to authenticated, service_role;

create trigger rights_reviews_enqueue_audio_lifecycle
after update of status on public.rights_reviews
for each row execute function public.enqueue_rights_lifecycle_handoff();

comment on function public.enqueue_rights_lifecycle_handoff() is
  'Atomically queues private approve/reject lifecycle jobs for quarantined assets after a rights decision; performs no object storage access.';

commit;
