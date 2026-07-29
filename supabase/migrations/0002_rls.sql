alter table sets enable row level security;
alter table artists enable row level security;
alter table events enable row level security;
alter table crews enable row level security;
alter table images enable row level security;
alter table field_candidates enable row level security;
alter table search_profiles enable row level security;
alter table heuristic_config enable row level security;
alter table user_roles enable row level security;

create or replace function has_role(required_role text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from user_roles
    where user_id = auth.uid()
      and role = required_role
  );
$$;

create policy "public read published" on sets
  for select using (review_status = 'published');

create policy "authenticated read reviewable sets" on sets
  for select to authenticated using (
    review_status in ('inbox','reviewing','accepted','published')
  );

create policy "editors update reviewable sets" on sets
  for update to authenticated using (
    has_role('editor') or has_role('admin')
  ) with check (
    has_role('editor') or has_role('admin')
  );

create policy "admins all sets" on sets
  for all to authenticated using (has_role('admin')) with check (has_role('admin'));

create policy "public read master data" on artists for select using (true);
create policy "public read events" on events for select using (true);
create policy "public read crews" on crews for select using (true);
create policy "public read linked images" on images for select using (true);

create policy "editors manage artists" on artists
  for all to authenticated using (has_role('editor') or has_role('admin'))
  with check (has_role('editor') or has_role('admin'));
create policy "editors manage events" on events
  for all to authenticated using (has_role('editor') or has_role('admin'))
  with check (has_role('editor') or has_role('admin'));
create policy "editors manage crews" on crews
  for all to authenticated using (has_role('editor') or has_role('admin'))
  with check (has_role('editor') or has_role('admin'));
create policy "editors manage images" on images
  for all to authenticated using (has_role('editor') or has_role('admin'))
  with check (has_role('editor') or has_role('admin'));
create policy "editors manage candidates" on field_candidates
  for all to authenticated using (has_role('editor') or has_role('admin'))
  with check (has_role('editor') or has_role('admin'));
create policy "admins manage profiles" on search_profiles
  for all to authenticated using (has_role('admin'))
  with check (has_role('admin'));
create policy "admins manage heuristics" on heuristic_config
  for all to authenticated using (has_role('admin')) with check (has_role('admin'));

create policy "users read own role" on user_roles
  for select to authenticated using (user_id = auth.uid() or has_role('admin'));
create policy "admins manage roles" on user_roles
  for all to authenticated using (has_role('admin')) with check (has_role('admin'));
