# SYCO23 SETCRAWLER production deployment

This runbook deploys FastAPI, Redis, four Celery workers, exactly one Celery
beat scheduler, and Caddy on one persistent Docker host. Nuxt remains on
Vercel. PostgreSQL and Auth remain on a dedicated Supabase project.

## Production topology

```text
Vercel Nuxt
    |
    | HTTPS + Supabase bearer JWT
    v
Caddy :443
    |
    v
FastAPI :8000 ---- Supabase Postgres
    |
    v
Redis AOF ---- YouTube / SoundCloud / FTM / process workers
    |
    v
exactly one Celery beat redriver
```

Do not deploy the local `db` or `web` services from `docker-compose.yml` on
the production host. Use `docker-compose.production.yml`.

## Current production target

The following values are public deployment coordinates, not secrets:

```dotenv
API_DOMAIN=api.syco23.org
ACME_EMAIL=emilach82@gmail.com
CORS_ORIGINS=https://syco23-setcrawler.vercel.app
SUPABASE_URL=https://smoevguhtsclfcmjwwhq.supabase.co
SUPABASE_JWT_AUDIENCE=authenticated
```

- Supabase project: `syco23-setcrawler` (`smoevguhtsclfcmjwwhq`), `eu-west-1`
- Frontend: `https://syco23-setcrawler.vercel.app`
- API host: IONOS VPS at `87.106.219.4`

Keep `SUPABASE_ANON_KEY`, the database password, the complete `DATABASE_URL`,
and provider credentials in their platform or host secret stores. Do not add
them to this runbook.

## Database decision

Do not apply the SETCRAWLER migrations to the existing
`event-live-set-database` project. That project already owns incompatible
`artists`, `events`, and `live_sets` tables for the flyer/OCR system.

Create a dedicated Supabase project named `syco23-setcrawler` in the selected
organization and region. For an IPv4-only persistent host, use the Supavisor
session pooler on port `5432`. Keep the database password and complete
`DATABASE_URL` on the backend host only.

Apply the migrations in this order:

```text
supabase/migrations/0001_init.sql
supabase/migrations/0002_rls.sql
supabase/migrations/0003_indexes.sql
supabase/migrations/20260728192205_provider_jobs.sql
supabase/migrations/20260729060000_final_release_fixes.sql
```

Afterward, run Supabase security and performance advisors. Confirm explicit
Data API grants separately from RLS.

## Host prerequisites

- A Linux host with Docker Engine and Docker Compose v2.
- DNS A/AAAA record for the API hostname pointing to the host.
- Public inbound TCP ports 80 and 443; UDP 443 is optional but enables HTTP/3.
- Outbound HTTPS access for Supabase Auth, provider metadata, and Caddy ACME.
- A non-root deployment directory such as `/opt/syco23-setcrawler`.

Do not continue if ports 80/443 are already owned by another reverse proxy.
Integrate the API route into that proxy instead of replacing unrelated
services.

## Configure

Copy the repository to the host, then create the private environment file:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

Set at least:

```dotenv
API_DOMAIN=api-setcrawler.example.com
ACME_EMAIL=operations@example.com
CORS_ORIGINS=https://syco23-setcrawler.vercel.app
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<publishable-or-anon-key>
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@<session-pooler>:5432/postgres?sslmode=require
PROVIDER_MODE=fixture
```

`SUPABASE_SERVICE_ROLE_KEY`, provider credentials, and `DATABASE_URL` must
never be placed in Nuxt or any `NUXT_PUBLIC_*` variable. Start with
`PROVIDER_MODE=fixture`; issue #5 owns credentialed provider smoke tests and
the later switch to `live`.

## Validate and start

```bash
docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  config --quiet

docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  build

docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  up -d
```

Check the topology:

```bash
docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  ps

docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  logs --tail=100 api worker-beat

curl --fail --silent --show-error \
  "https://${API_DOMAIN}/health"
```

The health response must be:

```json
{"status":"ok","service":"syco23-setcrawler-api"}
```

Confirm that only one `worker-beat` container is running. Redis and FastAPI
must not publish host ports; only Caddy publishes 80/443.

## Connect Vercel

Only after the HTTPS health check passes, set these Vercel production and
preview variables:

```dotenv
NUXT_PUBLIC_API_BASE=https://<api-domain>
NUXT_PUBLIC_RUNTIME_MODE=production
NUXT_PUBLIC_SUPABASE_URL=https://<project-ref>.supabase.co
NUXT_PUBLIC_SUPABASE_ANON_KEY=<publishable-or-anon-key>
```

Redeploy a PR #1 preview before promoting any artifact to production. Verify
magic-link auth, `/auth/me`, dashboard, inbox, import receipt/queue, search
profiles, and settings.

## Recovery drill

Run this only on the dedicated SETCRAWLER project:

1. Create one queued metadata import through the authenticated API.
2. Stop Redis without removing its volume.
3. Confirm the durable row remains in PostgreSQL.
4. Start Redis and the workers again.
5. Confirm Celery beat republishes the queued row.
6. Repeat with a due `retry` row and a `processing` row older than
   `JOB_CLAIM_TTL_SECONDS`.
7. Confirm each row is claimed once and the retry budget is preserved.

Never use `docker compose down -v` in production; that deletes the Redis AOF
volume.

## Rollback

1. Record the current image IDs and deployment commit before upgrading.
2. Build or pull the previous API/worker revision.
3. Stop only `api`, the four workers, and `worker-beat`.
4. Start the previous revision with the same `.env.production`.
5. Keep `redis_data`, `caddy_data`, and all PostgreSQL rows intact.
6. Re-run HTTPS health, auth identity, and one read-only set query.

The v0.2 recovery change adds no database table or column, so the previous
runtime can read existing `import_jobs`. Do not reverse migrations or delete
job history during rollback.
