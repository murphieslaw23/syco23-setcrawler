-- Release hardening for search-profile lifecycle and direct Data API authority.
alter table search_profiles
  add column if not exists deleted_at timestamptz;

alter table import_jobs
  drop constraint if exists import_jobs_search_profile_id_fkey;

alter table import_jobs
  add constraint import_jobs_search_profile_id_fkey
  foreign key (search_profile_id)
  references search_profiles(id)
  on delete restrict;

drop policy if exists "editors manage profiles" on search_profiles;
drop policy if exists "admins manage profiles" on search_profiles;

create policy "admins manage profiles" on search_profiles
  for all to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));
