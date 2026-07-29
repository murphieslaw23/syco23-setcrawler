# SYCO23 SETCRAWLER v0.2

**SYCO23** is the public editorial surface of the SYSTEM CORRUPT liveset
discovery platform. It collects metadata for long-form DJ sets and mixes,
scores likely sets, and puts every accepted record into an editorial inbox.

## What v0.2 includes

- Nuxt 3 operator UI: dashboard, inbox, set review, manual import receipt,
  import-job monitor, provider health, profiles, settings, artists, and events.
- FastAPI REST API with Supabase JWT or explicit non-production local-role
  authentication (`viewer`, `editor`, `admin`).
- PostgreSQL repository, Supabase migrations, RLS policies and explicit table
  grants; the deterministic memory repository is fixture-test-only.
- Redis/Celery queues that isolate YouTube, SoundCloud, and FreeTeknoMusic
  (FTM) fetching from the common processing worker, with Redis AOF persistence
  and a PostgreSQL-backed periodic redriver for lost broker deliveries.
- Official YouTube Data API search/profile and video-detail adapter.
- Manual SoundCloud metadata imports through a constrained `yt-dlp` subprocess.
- Conservative, opt-in FTM set-page adapter with `robots.txt` checks and a
  five-to-ten second request delay.
- Durable job states, bounded retries, duplicate detection, configurable set
  scoring, field candidates, and operator review controls.

## Deliberate non-goals

- No provider audio or video is downloaded or stored.
- No set is auto-published. Scoring can create an inbox record only; a separate
  editor action is required to publish.
- Routine tests and CI use fixtures; they do not call live providers.
- SoundCloud has no automated discovery/search. Only a validated, manually
  supplied track URL can enter the importer.
- FTM is disabled unless explicitly enabled. OCR, image processing and public
  SEO pages are outside this v0.2 release.

## Prerequisites

- Python 3.12
- Node.js 22+
- PostgreSQL 16 and Redis 7 for operational native mode, or Docker Compose
- A Supabase project for production authentication and database hosting

## Run locally

### Native fixture UI and API

The deterministic fixture mode is suitable for UI and contract exploration. It
does not need PostgreSQL, Redis, provider credentials, or a Supabase project.

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
ENVIRONMENT=fixture REPOSITORY_MODE=memory AUTH_MODE=local \
  PYTHONPATH=apps/api .venv/bin/uvicorn app.main:app --reload --port 8000
```

In another shell:

```bash
cd apps/web
npm ci
NUXT_PUBLIC_RUNTIME_MODE=fixture npm run dev
```

Open <http://localhost:3000>. Fixture mode exposes deterministic data and
rejects provider-import dispatches; it is not a simulation of live ingestion.

### Native operational mode

Start PostgreSQL and Redis, apply the migrations described below, then use a
local editor/admin role only outside production. `.env.example` is
Compose-first: its `db` and `redis` hostnames are Compose service names, so
native processes must replace those values in the copied `.env`. These values
are read by every API/worker terminal, so do not rely on a one-shell export.

```bash
cp .env.example .env
```

Edit the copied `.env` and replace its Compose hostnames with these persistent
native values:

```dotenv
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/syco23
REDIS_URL=redis://localhost:6379/0
```

Then start the API; each worker terminal reads the same edited `.env`:

```bash
PYTHONPATH=apps/api .venv/bin/uvicorn app.main:app --reload --port 8000
```

Run workers in separate terminals:

```bash
PYTHONPATH=apps/api .venv/bin/celery -A app.workers.celery_app:celery_app worker -Q youtube --concurrency=1 --loglevel=INFO
PYTHONPATH=apps/api .venv/bin/celery -A app.workers.celery_app:celery_app worker -Q soundcloud --concurrency=1 --loglevel=INFO
PYTHONPATH=apps/api .venv/bin/celery -A app.workers.celery_app:celery_app worker -Q ftm --concurrency=1 --loglevel=INFO
PYTHONPATH=apps/api .venv/bin/celery -A app.workers.celery_app:celery_app worker -Q process --concurrency=2 --loglevel=INFO
PYTHONPATH=apps/api .venv/bin/celery -A app.workers.celery_app:celery_app beat --loglevel=INFO
```

Use `PROVIDER_MODE=fixture` by default. Change it to `live` only after the
provider-specific configuration below is in place. The web app uses local-role
headers only when `NUXT_PUBLIC_RUNTIME_MODE=local`; it shows API errors rather
than treating failed operational writes as demo success.

### Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Compose starts web, API, PostgreSQL, AOF-backed Redis, four queue workers, and
Celery beat for database redrive. Its database initialization adds a small
Supabase compatibility shim then applies `0001_init.sql`, `0003_indexes.sql`,
the provider-job migration, and the final release migration. It intentionally
does **not** apply `0002_rls.sql`, because plain PostgreSQL does not provide
Supabase's `auth.uid()` runtime context. Use a Supabase project for the RLS
migration and real JWT operation.

### Persistent production host

The production backend is intentionally separate from the Vercel Nuxt
deployment. Use the production Compose file to run FastAPI, AOF-backed Redis,
the four provider/process workers, exactly one Celery beat scheduler, and a
Caddy HTTPS edge:

```bash
cp .env.production.example .env.production
docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  config --quiet
docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  up -d --build
```

The existing `event-live-set-database` Supabase project contains an
incompatible flyer/OCR schema and must not receive the SETCRAWLER migrations.
Use a dedicated Supabase project. See
[`docs/deployment-production.md`](docs/deployment-production.md) for database,
DNS/TLS, Vercel, recovery-drill, and non-destructive rollback instructions.

## Supabase production setup

1. Create the project and keep its database URL in `DATABASE_URL` for API and
   workers only. Use a pooled connection string where your Supabase setup
   requires it.
2. Apply migrations in this exact order:

   ```text
   supabase/migrations/0001_init.sql
   supabase/migrations/0002_rls.sql
   supabase/migrations/0003_indexes.sql
   supabase/migrations/20260728192205_provider_jobs.sql
   supabase/migrations/20260729060000_final_release_fixes.sql
   ```

3. Create the private storage buckets `flyers`, `thumbnails`, and
   `artist-images`. Image ingestion is not part of v0.2 yet, but the core
   schema expects those names.
4. Configure the API and workers with server-only `SUPABASE_URL`,
   `SUPABASE_ANON_KEY`, `DATABASE_URL`, and any server credentials. Configure
   the web separately with `NUXT_PUBLIC_SUPABASE_URL`,
   `NUXT_PUBLIC_SUPABASE_ANON_KEY`,
   `NUXT_PUBLIC_API_BASE=https://<your-api-host>`, and
   `NUXT_PUBLIC_RUNTIME_MODE=production`. The API verifies bearer JWTs against
   `<SUPABASE_URL>/auth/v1/.well-known/jwks.json` and resolves the application
   role from `user_roles`; it requires the `authenticated` audience and the
   canonical `<SUPABASE_URL>/auth/v1` issuer.
5. Create/invite a Supabase Auth user, then provision its role as a database
   administrator:

   ```sql
   insert into public.user_roles (user_id, role)
   values ('<auth.users UUID>', 'admin');
   ```

   Valid roles are `viewer`, `editor`, and `admin`. Do not use the service-role
   key in Nuxt. `SUPABASE_SERVICE_ROLE_KEY`, the database URL and
   `YOUTUBE_API_KEY` are server/worker secrets only.
6. For API/workers set `ENVIRONMENT=production`,
   `REPOSITORY_MODE=postgres`, and `AUTH_MODE=supabase`. For the web set the
   public runtime variables from step 4. Production API startup rejects
   `AUTH_MODE=local` and missing Supabase URL/anonymous-key configuration.

## Provider operation and boundaries

### YouTube

YouTube uses the official Data API v3 only. Set `YOUTUBE_API_KEY` in the API
and `worker-youtube` environment, set `PROVIDER_MODE=live`, and create enabled
search profiles in the operator UI. Search results request long videos and
fetch full video metadata before queue-isolated common processing. A missing
key leaves the provider unconfigured. The worker checkpoints each profile page
as idempotent child jobs; a fenced finalizer derives the parent's counts and
cursor only from those durable child outcomes.

### SoundCloud

SoundCloud accepts a manually submitted `https://soundcloud.com/<artist>/<track>`
URL only. The importer rejects credentials, ports, collection-style routes,
encoded path traversal and redirect-like query inputs. The worker executes
exactly metadata extraction (`--ignore-config --no-playlist --skip-download
--dump-single-json`): it never requests media download. The subprocess has a
30-second limit and independent 1 MiB stdout/stderr bounds. The SoundCloud
worker runs as a non-root user in the supplied image and Compose makes its root
filesystem read-only with a 64 MiB noexec tmpfs, 1 CPU and 512 MiB memory.

### FreeTeknoMusic

FTM is disabled by default. To opt in, set all of:

```dotenv
PROVIDER_MODE=live
FTM_SCRAPER_ENABLED=true
SCRAPER_USER_AGENT="syco23-setcrawler/0.1 (+contact: you@example.com)"
SCRAPER_REQUEST_DELAY_MS=5000
```

Only HTTPS `freeteknomusic.org/sets/<slug>` pages are valid. Before every page
request, the adapter fetches and evaluates `robots.txt`; unavailable, non-200,
or disallowed robots rules block the job. It uses the named User-Agent and
waits 5,000–10,000 ms between every HTTP request, including robots/page and
page/next-robots boundaries. It limits a crawl to 25 pages, stores raw HTML for
a fetched page, and does not fetch media. Do not enable it without a real
contact address and permission to crawl the target paths.

## Jobs, scoring and editorial flow

Jobs move through `queued`, `processing`, `retry`, `completed`, `failed`,
`blocked`, or `dead_letter`. Temporary processing failures use three bounded
delays: 5, 30 and 120 seconds; exhaustion becomes `dead_letter`. A claim lease
and `started_at` ownership token fence late deliveries. Replaying a completed
job returns its existing result instead of persisting another set.

Provider workers publish normalized payloads and the exact claim token to the
`process` queue; they do not run scoring or persistence inline. Tasks are
late-acknowledged and rejected on worker loss. An early redelivery schedules a
replacement for lease expiry without calling the provider, while an initial
broker-publish failure safely terminalizes the new job before the API returns
`503`. Retry claims cannot run before durable `next_retry_at`, and the retry
counter is persisted in job details so broker recreation cannot reset the
three-failure budget. Celery beat republishes queued, due-retry, and
lease-expired processing rows from PostgreSQL; duplicate publications remain
safe because the database claim is atomic and fenced. The common process
worker computes the configurable set score, detects duplicates first by stable
`(source, source_id)`, then canonical URL and
fingerprint, and creates candidate fields. Scores below acceptance are logged
as discarded jobs; qualifying records are created with
`review_status='inbox'`. Nothing in the importer changes a set to `published`.

Search profiles can be created, changed, run, or deleted only by admins. A
profile with an active queued, processing, or retry job returns `409` on
delete. Once its jobs are terminal, deletion hides the profile but preserves
the immutable job-to-profile audit relationship.

## Verification

Run from repository root after dependencies are installed:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests -q
PYTHONPATH=apps/api .venv/bin/python -m compileall apps/api/app

cd apps/web
npm test
npm run typecheck
npm run build
```

The optional PostgreSQL repository contract is skipped unless
`TEST_DATABASE_URL` points at a prepared PostgreSQL database with the
migrations applied. CI provisions PostgreSQL 16, Redis 7, Supabase-compatible
roles and `auth.uid()` before applying every migration.

Infrastructure checks:

```bash
rg -n "create table import_jobs|create table provider_cursors|enable row level security|grant .*authenticated" supabase/migrations
docker compose config
docker compose config --services
```

For a browser smoke test, install Playwright's browser runtime and run:

```bash
cd apps/web
npx playwright test
```

The checked-in test starts FastAPI in fixture/memory/local-auth mode and Nuxt
in fixture runtime, so it requires neither PostgreSQL nor live provider
credentials. It does require Playwright's Chromium binary. The last local run
reached Chromium launch and was blocked only because that binary was absent;
it is not downloaded automatically by this project command.

## Project map

- `apps/web` — Nuxt editorial UI and browser tests.
- `apps/api` — FastAPI API, provider adapters, durable processing, and tests.
- `supabase/migrations` — PostgreSQL, RLS, grants and indexes.
- `docker` / `docker-compose.yml` — local container runtime.
- `.github/workflows/ci.yml` — PostgreSQL/Redis-backed API and Nuxt checks.
- `docs/architecture.md` — operational topology and safety model.
- `docs/superpowers` — approved design and implementation plans.
