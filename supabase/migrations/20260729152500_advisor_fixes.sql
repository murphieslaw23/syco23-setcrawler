-- Clear actionable Supabase security and performance advisor findings.

alter function public.set_updated_at() set search_path = '';

create index artists_image_id_idx on public.artists (image_id);
create index crews_image_id_idx on public.crews (image_id);
create index events_flyer_image_id_idx on public.events (flyer_image_id);
create index import_jobs_result_set_id_idx on public.import_jobs (result_set_id);
create index import_log_set_id_idx on public.import_log (set_id);
create index set_artists_artist_id_idx on public.set_artists (artist_id);
create index set_crews_crew_id_idx on public.set_crews (crew_id);
create index set_events_event_id_idx on public.set_events (event_id);
create index set_images_image_id_idx on public.set_images (image_id);

drop policy if exists "public read published" on public.sets;
drop policy if exists "authenticated read reviewable sets" on public.sets;
drop policy if exists "editors update reviewable sets" on public.sets;
drop policy if exists "admins all sets" on public.sets;

create policy "public read published" on public.sets
  for select to anon
  using (review_status = 'published');
create policy "authenticated read reviewable sets" on public.sets
  for select to authenticated
  using (
    review_status in ('inbox', 'reviewing', 'accepted', 'published')
    or private.has_role('admin')
  );
create policy "editors update reviewable sets" on public.sets
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "admins insert sets" on public.sets
  for insert to authenticated
  with check (private.has_role('admin'));
create policy "admins delete sets" on public.sets
  for delete to authenticated
  using (private.has_role('admin'));

drop policy if exists "public read master data" on public.artists;
drop policy if exists "editors manage artists" on public.artists;
create policy "public read artists" on public.artists
  for select to anon using (true);
create policy "authenticated read artists" on public.artists
  for select to authenticated using (true);
create policy "editors insert artists" on public.artists
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update artists" on public.artists
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete artists" on public.artists
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read events" on public.events;
drop policy if exists "editors manage events" on public.events;
create policy "public read events" on public.events
  for select to anon using (true);
create policy "authenticated read events" on public.events
  for select to authenticated using (true);
create policy "editors insert events" on public.events
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update events" on public.events
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete events" on public.events
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read crews" on public.crews;
drop policy if exists "editors manage crews" on public.crews;
create policy "public read crews" on public.crews
  for select to anon using (true);
create policy "authenticated read crews" on public.crews
  for select to authenticated using (true);
create policy "editors insert crews" on public.crews
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update crews" on public.crews
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete crews" on public.crews
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read linked images" on public.images;
drop policy if exists "editors manage images" on public.images;
create policy "public read linked images" on public.images
  for select to anon
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
create policy "authenticated read images" on public.images
  for select to authenticated using (true);
create policy "editors insert images" on public.images
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update images" on public.images
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete images" on public.images
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read published set artists" on public.set_artists;
drop policy if exists "authenticated read reviewable set artists" on public.set_artists;
drop policy if exists "editors manage set artists" on public.set_artists;
create policy "public read published set artists" on public.set_artists
  for select to anon
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
    or private.has_role('admin')
  );
create policy "editors insert set artists" on public.set_artists
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update set artists" on public.set_artists
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete set artists" on public.set_artists
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read published set events" on public.set_events;
drop policy if exists "authenticated read reviewable set events" on public.set_events;
drop policy if exists "editors manage set events" on public.set_events;
create policy "public read published set events" on public.set_events
  for select to anon
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
    or private.has_role('admin')
  );
create policy "editors insert set events" on public.set_events
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update set events" on public.set_events
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete set events" on public.set_events
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read published set crews" on public.set_crews;
drop policy if exists "authenticated read reviewable set crews" on public.set_crews;
drop policy if exists "editors manage set crews" on public.set_crews;
create policy "public read published set crews" on public.set_crews
  for select to anon
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
    or private.has_role('admin')
  );
create policy "editors insert set crews" on public.set_crews
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update set crews" on public.set_crews
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete set crews" on public.set_crews
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "public read published set images" on public.set_images;
drop policy if exists "authenticated read reviewable set images" on public.set_images;
drop policy if exists "editors manage set images" on public.set_images;
create policy "public read published set images" on public.set_images
  for select to anon
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
    or private.has_role('admin')
  );
create policy "editors insert set images" on public.set_images
  for insert to authenticated
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors update set images" on public.set_images
  for update to authenticated
  using (private.has_role('editor') or private.has_role('admin'))
  with check (private.has_role('editor') or private.has_role('admin'));
create policy "editors delete set images" on public.set_images
  for delete to authenticated
  using (private.has_role('editor') or private.has_role('admin'));

drop policy if exists "admins read provider cursors" on public.provider_cursors;
drop policy if exists "admins manage provider cursors" on public.provider_cursors;
create policy "admins read provider cursors" on public.provider_cursors
  for select to authenticated
  using (private.has_role('admin'));
create policy "admins insert provider cursors" on public.provider_cursors
  for insert to authenticated
  with check (private.has_role('admin'));
create policy "admins update provider cursors" on public.provider_cursors
  for update to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));
create policy "admins delete provider cursors" on public.provider_cursors
  for delete to authenticated
  using (private.has_role('admin'));

drop policy if exists "users read own role" on public.user_roles;
drop policy if exists "admins manage roles" on public.user_roles;
create policy "users read own role" on public.user_roles
  for select to authenticated
  using (user_id = (select auth.uid()) or private.has_role('admin'));
create policy "admins insert roles" on public.user_roles
  for insert to authenticated
  with check (private.has_role('admin'));
create policy "admins update roles" on public.user_roles
  for update to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));
create policy "admins delete roles" on public.user_roles
  for delete to authenticated
  using (private.has_role('admin'));
