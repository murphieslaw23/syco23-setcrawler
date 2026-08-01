-- v0.5 canonical set merge review. Provider sources are moved, never deleted.

begin;

alter table public.sets
  add column if not exists merged_into_id uuid,
  add column if not exists merged_at timestamptz;

alter table public.sets
  add constraint sets_merged_into_fk
    foreign key (merged_into_id)
    references public.sets(id)
    on delete restrict,
  add constraint sets_not_merged_into_self
    check (merged_into_id is null or merged_into_id <> id),
  add constraint sets_merge_timestamp_consistent
    check ((merged_into_id is null) = (merged_at is null));

create table public.merge_candidates (
  id uuid primary key default gen_random_uuid(),
  source_set_id uuid not null
    references public.sets(id) on delete restrict,
  target_set_id uuid not null
    references public.sets(id) on delete restrict,
  score numeric(7,6) not null check (score between 0 and 1),
  component_scores jsonb not null
    check (jsonb_typeof(component_scores) = 'object'),
  reasons jsonb not null default '[]'::jsonb
    check (jsonb_typeof(reasons) = 'array'),
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected', 'restored')),
  reviewed_by text check (
    reviewed_by is null or char_length(reviewed_by) between 1 and 300
  ),
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (source_set_id <> target_set_id),
  check ((reviewed_by is null) = (reviewed_at is null))
);

create unique index merge_candidates_unordered_pair_idx
  on public.merge_candidates (
    least(source_set_id, target_set_id),
    greatest(source_set_id, target_set_id)
  );
create index merge_candidates_review_queue_idx
  on public.merge_candidates (status, score desc, created_at);

create table public.merge_decisions (
  id uuid primary key default gen_random_uuid(),
  merge_candidate_id uuid not null
    references public.merge_candidates(id) on delete restrict,
  action text not null check (action in ('approve', 'reject', 'restore')),
  actor text not null check (char_length(actor) between 1 and 300),
  before_state jsonb not null
    check (jsonb_typeof(before_state) = 'object'),
  after_state jsonb not null
    check (jsonb_typeof(after_state) = 'object'),
  created_at timestamptz not null default now()
);

create index merge_decisions_candidate_created_idx
  on public.merge_decisions (merge_candidate_id, created_at, id);

create or replace function public.prevent_merge_decision_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'merge decisions are immutable';
end
$$;

create trigger merge_decisions_immutable
before update or delete on public.merge_decisions
for each row execute function public.prevent_merge_decision_mutation();

create trigger merge_candidates_updated_at
before update on public.merge_candidates
for each row execute function public.set_updated_at();

alter table public.merge_candidates enable row level security;
alter table public.merge_decisions enable row level security;

revoke all on table public.merge_candidates from anon, authenticated, service_role;
revoke all on table public.merge_decisions from anon, authenticated, service_role;

grant select, insert, update
  on table public.merge_candidates
  to service_role;
grant select, insert
  on table public.merge_decisions
  to service_role;
grant select, insert, update
  on table public.merge_candidates
  to authenticated;
grant select, insert
  on table public.merge_decisions
  to authenticated;

create policy "admins manage merge candidates" on public.merge_candidates
  for all to authenticated
  using (private.has_role('admin'))
  with check (private.has_role('admin'));
create policy "admins read merge decisions" on public.merge_decisions
  for select to authenticated
  using (private.has_role('admin'));
create policy "admins append merge decisions" on public.merge_decisions
  for insert to authenticated
  with check (private.has_role('admin'));

comment on table public.merge_candidates is
  'Explainable cross-provider canonical-set suggestions requiring admin review.';
comment on table public.merge_decisions is
  'Append-only before/after audit evidence for merge review actions.';

commit;
