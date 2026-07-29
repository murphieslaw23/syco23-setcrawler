-- Make Data API exposure explicit for Supabase projects created after 2026-05-30.
-- Grants decide which objects a role can reach; RLS decides which rows it can use.

alter table public.sets enable row level security;
alter table public.artists enable row level security;
alter table public.events enable row level security;
alter table public.crews enable row level security;
alter table public.images enable row level security;
alter table public.set_artists enable row level security;
alter table public.set_events enable row level security;
alter table public.set_crews enable row level security;
alter table public.set_images enable row level security;
alter table public.field_candidates enable row level security;
alter table public.import_log enable row level security;
alter table public.search_profiles enable row level security;
alter table public.user_roles enable row level security;
alter table public.heuristic_config enable row level security;
alter table public.import_jobs enable row level security;
alter table public.provider_cursors enable row level security;

revoke all on table public.sets from anon, authenticated, service_role;
revoke all on table public.artists from anon, authenticated, service_role;
revoke all on table public.events from anon, authenticated, service_role;
revoke all on table public.crews from anon, authenticated, service_role;
revoke all on table public.images from anon, authenticated, service_role;
revoke all on table public.set_artists from anon, authenticated, service_role;
revoke all on table public.set_events from anon, authenticated, service_role;
revoke all on table public.set_crews from anon, authenticated, service_role;
revoke all on table public.set_images from anon, authenticated, service_role;
revoke all on table public.field_candidates from anon, authenticated, service_role;
revoke all on table public.import_log from anon, authenticated, service_role;
revoke all on table public.search_profiles from anon, authenticated, service_role;
revoke all on table public.user_roles from anon, authenticated, service_role;
revoke all on table public.heuristic_config from anon, authenticated, service_role;
revoke all on table public.import_jobs from anon, authenticated, service_role;
revoke all on table public.provider_cursors from anon, authenticated, service_role;

grant usage on schema public to anon, authenticated, service_role;

grant select on table public.sets to anon;
grant select on table public.artists to anon;
grant select on table public.events to anon;
grant select on table public.crews to anon;
grant select on table public.images to anon;
grant select on table public.set_artists to anon;
grant select on table public.set_events to anon;
grant select on table public.set_crews to anon;
grant select on table public.set_images to anon;

grant select, insert, update, delete
  on table public.sets,
    public.artists,
    public.events,
    public.crews,
    public.images,
    public.set_artists,
    public.set_events,
    public.set_crews,
    public.set_images,
    public.field_candidates,
    public.search_profiles,
    public.user_roles,
    public.heuristic_config
  to authenticated;
grant select on table public.import_log to authenticated;
grant select, insert, update on table public.import_jobs to authenticated;
grant select, insert, update, delete on table public.provider_cursors to authenticated;

grant select, insert, update, delete
  on table public.sets,
    public.artists,
    public.events,
    public.crews,
    public.images,
    public.set_artists,
    public.set_events,
    public.set_crews,
    public.set_images,
    public.field_candidates,
    public.import_log,
    public.search_profiles,
    public.user_roles,
    public.heuristic_config,
    public.import_jobs,
    public.provider_cursors
  to service_role;

drop policy if exists "public read linked images" on public.images;
create policy "public read linked images" on public.images
  for select to anon, authenticated
  using (
    exists (
      select 1
      from public.set_images
      join public.sets on sets.id = set_images.set_id
      where set_images.image_id = images.id
        and sets.review_status = 'published'
    )
    or exists (select 1 from public.artists where artists.image_id = images.id)
    or exists (select 1 from public.events where events.flyer_image_id = images.id)
    or exists (select 1 from public.crews where crews.image_id = images.id)
  );

create policy "public read published set artists" on public.set_artists
  for select to anon, authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_artists.set_id
        and sets.review_status = 'published'
    )
  );
create policy "authenticated read reviewable set artists" on public.set_artists
  for select to authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_artists.set_id
        and sets.review_status in ('inbox', 'reviewing', 'accepted', 'published')
    )
  );
create policy "editors manage set artists" on public.set_artists
  for all to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));

create policy "public read published set events" on public.set_events
  for select to anon, authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_events.set_id
        and sets.review_status = 'published'
    )
  );
create policy "authenticated read reviewable set events" on public.set_events
  for select to authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_events.set_id
        and sets.review_status in ('inbox', 'reviewing', 'accepted', 'published')
    )
  );
create policy "editors manage set events" on public.set_events
  for all to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));

create policy "public read published set crews" on public.set_crews
  for select to anon, authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_crews.set_id
        and sets.review_status = 'published'
    )
  );
create policy "authenticated read reviewable set crews" on public.set_crews
  for select to authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_crews.set_id
        and sets.review_status in ('inbox', 'reviewing', 'accepted', 'published')
    )
  );
create policy "editors manage set crews" on public.set_crews
  for all to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));

create policy "public read published set images" on public.set_images
  for select to anon, authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_images.set_id
        and sets.review_status = 'published'
    )
  );
create policy "authenticated read reviewable set images" on public.set_images
  for select to authenticated
  using (
    exists (
      select 1 from public.sets
      where sets.id = set_images.set_id
        and sets.review_status in ('inbox', 'reviewing', 'accepted', 'published')
    )
  );
create policy "editors manage set images" on public.set_images
  for all to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));

create policy "editors read import log" on public.import_log
  for select to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "users read own role" on public.user_roles;
create policy "users read own role" on public.user_roles
  for select to authenticated
  using (user_id = (select auth.uid()) or private.has_role('admin'));
