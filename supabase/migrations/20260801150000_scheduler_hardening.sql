alter table public.search_profiles
  add column if not exists schedule_timezone text not null default 'UTC';

alter table public.search_profiles
  add constraint search_profiles_schedule_timezone_name
    check (
      schedule_timezone = 'UTC'
      or schedule_timezone ~ '^[A-Za-z][A-Za-z0-9_+.-]*/[A-Za-z0-9_+./-]+$'
    );

-- A dormant system profile provides the weekly metadata crawl. The scheduler
-- will only queue it when the ftm descriptor is effectively enabled through
-- FTM_SCRAPER_ENABLED and all of its required settings are present.
insert into public.search_profiles (
  id,
  name,
  query,
  source,
  operation,
  parameters,
  schedule_cron,
  schedule_timezone,
  enabled
) values (
  '00000000-0000-4000-8000-000000000007',
  'Weekly FTM metadata crawl',
  'freeteknomusic metadata crawl',
  'ftm',
  'crawl',
  '{"start_url":"https://freeteknomusic.org/sets/23hz"}'::jsonb,
  '0 4 * * 1',
  'UTC',
  true
)
on conflict (id) do nothing;
