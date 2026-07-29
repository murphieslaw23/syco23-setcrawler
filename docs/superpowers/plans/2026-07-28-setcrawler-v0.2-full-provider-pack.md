# SYCO23 SETCRAWLER v0.2 Full Provider Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the production-shaped v0.2 ingestion vertical slice with PostgreSQL persistence, Supabase Auth roles, Redis/Celery jobs, three metadata-only provider adapters, and operator UI.

**Architecture:** FastAPI owns authentication, durable job creation, and editorial REST contracts. PostgreSQL is the source of truth; Celery workers receive job UUIDs through Redis, fetch provider metadata, and pass `RawSetPayload` records through one idempotent normalize/deduplicate/score/enrich pipeline. Nuxt uses Supabase sessions in production and an explicit local role only outside production.

**Tech Stack:** Python 3.12, FastAPI 0.116.1, Pydantic 2.13.4, psycopg 3, Celery 5.6, Redis 7, PyJWT, httpx, yt-dlp, Nuxt 3.21.10, Vue 3.5.40, TypeScript 5.9, Supabase Auth/Postgres, Vitest, pytest.

## Global Constraints

- Project name is `syco23-setcrawler`.
- Public UI name is **SYCO23**; formal brand is **SYSTEM CORRUPT**.
- Never use BASSLIVE, BLR23, vinyl-player metaphors, or terminal/console metaphors.
- Never download provider audio or video.
- Never auto-publish a set.
- SoundCloud accepts manually submitted URLs only.
- FTM is disabled unless `FTM_SCRAPER_ENABLED=true`.
- FTM must honor `robots.txt` and `SCRAPER_REQUEST_DELAY_MS`.
- Provider adapters expose `async def fetch(url: str) -> RawSetPayload`.
- Live provider calls never run in routine CI.
- Production startup rejects `AUTH_MODE=local`.
- `SUPABASE_SERVICE_ROLE_KEY` and `YOUTUBE_API_KEY` remain server-only.
- Node.js floor is 22 and Python version is 3.12.
- PostgreSQL and Python/Node dependencies are version-pinned.
- New Supabase tables receive explicit grants as well as RLS policies because automatic Data API exposure changed in 2026.

---

## File Structure

### API files to create

- `apps/api/app/core/database.py` — psycopg connection-pool lifecycle.
- `apps/api/app/core/auth.py` — Supabase JWT verification and local-mode identity.
- `apps/api/app/schemas/auth.py` — typed user roles and current identity.
- `apps/api/app/repositories/base.py` — repository protocol shared by API and workers.
- `apps/api/app/repositories/memory.py` — relocated deterministic repository for tests.
- `apps/api/app/repositories/postgres.py` — transactional PostgreSQL implementation.
- `apps/api/app/services/provider.py` — common adapter protocol and provider errors.
- `apps/api/app/services/youtube.py` — official YouTube Data API adapter and profile search.
- `apps/api/app/services/soundcloud.py` — validated metadata-only yt-dlp adapter.
- `apps/api/app/services/ftm.py` — robots-aware, delayed FTM metadata adapter.
- `apps/api/app/services/import_pipeline.py` — idempotent normalize/score/enrich/persist orchestration.
- `apps/api/app/workers/celery_app.py` — Celery configuration and routing.
- `apps/api/app/workers/dispatch.py` — API-facing job dispatcher.
- `apps/api/app/workers/youtube_poller.py` — YouTube profile task.
- `apps/api/app/workers/soundcloud_importer.py` — SoundCloud URL task.
- `apps/api/app/workers/ftm_scraper.py` — FTM URL/crawl task.
- `apps/api/app/workers/normalize_worker.py` — common processing task.
- `apps/api/app/routers/auth.py` — current identity endpoint.
- `apps/api/app/routers/providers.py` — provider health/configuration endpoint.
- `apps/api/tests/conftest.py` — reusable app, auth, and fixture setup.
- `apps/api/tests/fixtures/youtube_search.json` — offline YouTube search response.
- `apps/api/tests/fixtures/youtube_videos.json` — offline YouTube detail response.
- `apps/api/tests/fixtures/soundcloud.json` — offline yt-dlp metadata.
- `apps/api/tests/fixtures/ftm_set.html` — offline FTM set page.
- `apps/api/tests/test_auth.py` — role and production-mode tests.
- `apps/api/tests/test_jobs.py` — job-state and idempotency tests.
- `apps/api/tests/test_youtube.py` — YouTube request/normalization tests.
- `apps/api/tests/test_soundcloud.py` — URL and subprocess safety tests.
- `apps/api/tests/test_ftm.py` — robots, delay, and extraction tests.
- `apps/api/tests/test_fixture_pipelines.py` — end-to-end offline provider pipelines.
- `apps/api/tests/test_postgres_repository.py` — optional/integration repository contract.

### API files to modify

- `apps/api/app/core/config.py` — runtime, auth, Redis, timeout, and crawl settings.
- `apps/api/app/core/dependencies.py` — repository and role dependencies.
- `apps/api/app/main.py` — database lifecycle and new routers.
- `apps/api/app/repository.py` — compatibility re-exports.
- `apps/api/app/schemas/import_job.py` — durable state and job detail types.
- `apps/api/app/schemas/profile.py` — latest-run metadata.
- `apps/api/app/schemas/set.py` — score reasons and duplicate/job context.
- `apps/api/app/schemas/__init__.py` — new exports.
- `apps/api/app/routers/imports.py` — dispatch and retry routes.
- `apps/api/app/routers/search_profiles.py` — protected CRUD and dispatch.
- `apps/api/app/routers/sets.py` — role-protected mutations.
- `apps/api/app/routers/candidates.py` — role-protected decisions.
- `apps/api/app/routers/stats.py` — real job counts.
- `apps/api/app/services/normalizer.py` — provider aliases, dates, canonical URLs.
- `apps/api/app/services/heuristic.py` — persisted config mapping and score breakdown.
- `apps/api/app/services/enricher.py` — extraction coverage.
- `apps/api/requirements.txt` — pinned runtime and test dependencies.

### Web files to create

- `apps/web/plugins/supabase.client.ts` — browser Supabase client.
- `apps/web/composables/useAuth.ts` — session, role, and API auth headers.
- `apps/web/utils/auth.ts` — pure role capability mapping.
- `apps/web/utils/jobs.ts` — pure job filtering and retry rules.
- `apps/web/components/JobStateBadge.vue` — consistent job-state display.
- `apps/web/components/ProviderHealth.vue` — provider configuration card.
- `apps/web/pages/imports/index.vue` — durable job monitor.
- `apps/web/pages/login.vue` — Supabase magic-link sign-in.
- `apps/web/tests/unit/jobs.test.ts` — job filters and retry contracts.
- `apps/web/tests/unit/auth.test.ts` — role capability contracts.

### Web files to modify

- `apps/web/composables/useApi.ts` — bearer/local auth and typed errors.
- `apps/web/types/index.ts` — import, provider, auth, and score types.
- `apps/web/data/demo.ts` — deterministic v0.2 job/provider fixtures.
- `apps/web/data/navigation.ts` — import monitor navigation.
- `apps/web/pages/index.vue` — provider health and real queue metrics.
- `apps/web/pages/import.vue` — durable job receipt.
- `apps/web/pages/search-profiles/index.vue` — full CRUD/run state.
- `apps/web/pages/inbox/index.vue` — duplicate/job context.
- `apps/web/pages/inbox/[id].vue` — score evidence and role-gated actions.
- `apps/web/pages/settings.vue` — auth/provider state.
- `apps/web/app.vue` — identity/session affordance.
- `apps/web/assets/app.css` — v0.2 responsive components.
- `apps/web/nuxt.config.ts` — Supabase and auth public settings.
- `apps/web/package.json` and `apps/web/package-lock.json` — pinned Supabase client.

### Infrastructure and documentation

- `supabase/migrations/<generated>_provider_jobs.sql` — jobs, cursors, grants, RLS, indexes.
- `docker-compose.yml` — API, web, database, Redis, and four Celery worker processes.
- `docker/api.Dockerfile` — API runtime.
- `docker/worker.Dockerfile` — worker runtime with pinned yt-dlp.
- `.env.example` — all v0.2 settings.
- `.github/workflows/ci.yml` — PostgreSQL/Redis integration services and all checks.
- `README.md` — v0.2 setup and provider operation.
- `docs/architecture.md` — v0.2 topology, state flow, and safety.

---

### Task 1: Durable Domain Contracts and Migration

**Files:**
- Create: `supabase/migrations/<generated>_provider_jobs.sql`
- Create: `apps/api/app/repositories/base.py`
- Create: `apps/api/app/services/provider.py`
- Create: `apps/api/app/schemas/auth.py`
- Modify: `apps/api/app/schemas/import_job.py`
- Modify: `apps/api/app/schemas/profile.py`
- Modify: `apps/api/app/schemas/set.py`
- Modify: `apps/api/app/schemas/__init__.py`
- Test: `apps/api/tests/test_jobs.py`

**Interfaces:**
- Produces: `JobStatus`, `JobType`, `ImportJob`, `ImportJobPage`, `ImportJobPatch`.
- Produces: `UserRole` values `viewer`, `editor`, and `admin`.
- Produces: `Repository` protocol consumed by every router, worker, and repository.
- Produces: `ProviderAdapter` and typed provider exceptions.
- Produces: SQL tables `import_jobs` and `provider_cursors`.

- [ ] **Step 1: Create the migration through the Supabase CLI**

Run:

```bash
npx supabase --version
npx supabase migration new provider_jobs
```

Expected: one timestamped SQL file appears in `supabase/migrations`.

- [ ] **Step 2: Write failing schema and job-state tests**

Add:

```python
def test_job_contract_supports_operational_states() -> None:
    job = ImportJob(
        url="https://soundcloud.com/syco23/ritual",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
        status=JobStatus.blocked,
        attempt_count=1,
        error_code="provider_disabled",
        error_message="Provider is disabled",
    )
    assert job.status is JobStatus.blocked
    assert job.result_set_id is None


def test_job_transition_rejects_completed_to_processing() -> None:
    with pytest.raises(ValueError, match="completed"):
        validate_job_transition(JobStatus.completed, JobStatus.processing)
```

Run:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_jobs.py -q
```

Expected: FAIL because the new enums and transition validator do not exist.

- [ ] **Step 3: Implement typed job contracts**

Use these exact states:

```python
class JobStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    retry = "retry"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    dead_letter = "dead_letter"


class JobType(StrEnum):
    url_import = "url_import"
    search_profile = "search_profile"
    crawl = "crawl"


ALLOWED_JOB_TRANSITIONS = {
    JobStatus.queued: {JobStatus.processing, JobStatus.failed, JobStatus.blocked},
    JobStatus.processing: {JobStatus.completed, JobStatus.retry, JobStatus.failed, JobStatus.blocked},
    JobStatus.retry: {JobStatus.processing, JobStatus.dead_letter},
    JobStatus.completed: set(),
    JobStatus.failed: set(),
    JobStatus.blocked: set(),
    JobStatus.dead_letter: set(),
}
```

`ImportJob` must include `attempt_count`, `created_at`, `started_at`,
`finished_at`, `next_retry_at`, `result_set_id`, `error_code`,
`error_message`, and `details`.

- [ ] **Step 4: Define the repository protocol**

The protocol must include exact signatures used later:

```python
class Repository(Protocol):
    def create_job(self, *, url: str | None, source: SetSource, job_type: JobType,
                   profile_id: UUID | None = None, details: dict[str, Any] | None = None) -> ImportJob: ...
    def get_job(self, job_id: UUID) -> ImportJob | None: ...
    def list_jobs(self, *, source: SetSource | None, status: JobStatus | None,
                  limit: int, offset: int) -> ImportJobPage: ...
    def transition_job(self, job_id: UUID, patch: ImportJobPatch) -> ImportJob | None: ...
    def complete_duplicate_job(self, job_id: UUID, duplicate_set_id: UUID) -> ImportJob: ...
    def complete_discarded_job(self, job_id: UUID, score: ScoreResult) -> ImportJob: ...
    def find_duplicate(self, payload: RawSetPayload, fingerprint: str) -> UUID | None: ...
    def persist_processed_set(self, *, payload: RawSetPayload, score: ScoreResult,
                              candidates: list[CandidateCreate], job_id: UUID,
                              fingerprint: str) -> UUID: ...
    def get_heuristic_config(self) -> HeuristicConfig: ...
    def get_user_role(self, user_id: UUID) -> UserRole | None: ...
    def get_profile(self, profile_id: UUID) -> SearchProfile | None: ...
    def update_profile_run(self, profile_id: UUID, *, next_page_token: str | None,
                           result_count: int, error_code: str | None) -> SearchProfile: ...
```

Retain the existing set, candidate, profile, and stats methods in the protocol.

Define the role enum before the protocol consumes it:

```python
class UserRole(StrEnum):
    viewer = "viewer"
    editor = "editor"
    admin = "admin"
```

Define common provider types:

```python
class ProviderAdapter(Protocol):
    async def fetch(self, url: str) -> RawSetPayload: ...


class ProviderError(RuntimeError):
    code = "provider_error"
    retryable = False


class ProviderValidationError(ProviderError):
    code = "provider_validation"


class ProviderBlockedError(ProviderError):
    code = "provider_blocked"


class ProviderQuotaError(ProviderError):
    code = "provider_quota"


class ProviderTemporaryError(ProviderError):
    code = "provider_temporary"
    retryable = True


class ProviderPayloadError(ProviderError):
    code = "provider_payload"
```

Add `score_reasons: list[str]`, `import_job_id: UUID | None`, and
`duplicate_of_id: UUID | None` to set response models. Add
`last_result_count`, `last_error_code`, and `latest_job_id` to
`SearchProfile`.

- [ ] **Step 5: Implement the SQL migration**

The migration must create:

```sql
create table import_jobs (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('youtube','soundcloud','freeteknomusic')),
  job_type text not null check (job_type in ('url_import','search_profile','crawl')),
  input_url text,
  search_profile_id uuid references search_profiles(id) on delete set null,
  status text not null default 'queued'
    check (status in ('queued','processing','retry','completed','failed','blocked','dead_letter')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  started_at timestamptz,
  finished_at timestamptz,
  next_retry_at timestamptz,
  result_set_id uuid references sets(id) on delete set null,
  error_code text,
  error_message text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table provider_cursors (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('youtube','soundcloud','freeteknomusic')),
  cursor_key text not null,
  cursor_value text,
  last_success_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (source, cursor_key)
);

create index import_jobs_status_created_idx on import_jobs (status, created_at desc);
create index import_jobs_source_created_idx on import_jobs (source, created_at desc);
create index import_jobs_profile_idx on import_jobs (search_profile_id, created_at desc)
  where search_profile_id is not null;
create index sets_canonical_url_idx on sets (canonical_url);
create index sets_fingerprint_idx on sets ((raw_payload->>'duplicate_fingerprint'))
  where raw_payload ? 'duplicate_fingerprint';
```

Enable RLS on both tables. Grant explicit table access to `authenticated`,
grant sequence access where applicable, allow authenticated users to select,
allow editors/admins to insert jobs, and allow admins to update terminal jobs.
Provider cursors are admin-only. Use `TO authenticated`, `USING`, and
`WITH CHECK`; do not authorize from `user_metadata`.

Harden the existing role helper by moving it to a non-exposed `private` schema:

```sql
create schema if not exists private;
revoke all on schema private from public;
grant usage on schema private to authenticated;

create or replace function private.has_role(required_role text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.user_roles
    where user_id = (select auth.uid())
      and role = required_role
  );
$$;

revoke all on function private.has_role(text) from public;
grant execute on function private.has_role(text) to authenticated;
```

Recreate affected policies to call `private.has_role`. Drop the old
`public.has_role(text)` only after no policy depends on it.

- [ ] **Step 6: Run contract tests and inspect migration**

Run:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_jobs.py -q
rg -n "enable row level security|grant .*authenticated|with check|import_jobs_status_created_idx" supabase/migrations
```

Expected: tests PASS and every expected security/index clause is present.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/schemas apps/api/app/repositories/base.py apps/api/tests/test_jobs.py supabase/migrations
git commit -m "feat: add durable import job contracts"
```

---

### Task 2: PostgreSQL Repository and Supabase Auth

**Files:**
- Create: `apps/api/app/core/database.py`
- Create: `apps/api/app/core/auth.py`
- Create: `apps/api/app/repositories/memory.py`
- Create: `apps/api/app/repositories/postgres.py`
- Create: `apps/api/app/routers/auth.py`
- Create: `apps/api/tests/conftest.py`
- Create: `apps/api/tests/test_auth.py`
- Create: `apps/api/tests/test_postgres_repository.py`
- Modify: `apps/api/app/repository.py`
- Modify: `apps/api/app/core/config.py`
- Modify: `apps/api/app/core/dependencies.py`
- Modify: `apps/api/app/main.py`
- Modify: `apps/api/app/routers/sets.py`
- Modify: `apps/api/app/routers/candidates.py`
- Modify: `apps/api/requirements.txt`

**Interfaces:**
- Consumes: `Repository`, job types, and migration from Task 1.
- Produces: `CurrentUser(user_id: UUID, role: UserRole)`.
- Produces: `require_viewer`, `require_editor`, and `require_admin`.
- Produces: `PostgresRepository(pool: ConnectionPool)`.

- [ ] **Step 1: Pin database and JWT dependencies**

Add exact packages:

```text
psycopg[binary,pool]==3.2.10
PyJWT[crypto]==2.10.1
```

Create the environment, install requirements, and compile:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
PYTHONPATH=apps/api .venv/bin/python -m compileall apps/api/app
```

- [ ] **Step 2: Write failing auth and repository-selection tests**

Add tests:

```python
def test_production_rejects_local_auth() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=local"):
        Settings(environment="production", auth_mode="local")


def test_editor_dependency_rejects_viewer(client_as_viewer: TestClient) -> None:
    response = client_as_viewer.post(
        "/imports/url",
        json={"url": "https://soundcloud.com/syco23/ritual"},
    )
    assert response.status_code == 403


def test_editor_can_change_candidate(client_as_editor: TestClient) -> None:
    set_id, candidate_id = seeded_candidate_ids(client_as_editor)
    assert client_as_editor.post(
        f"/sets/{set_id}/candidates/{candidate_id}/accept"
    ).status_code == 200
```

Run:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_auth.py -q
```

Expected: FAIL because settings validation and role dependencies do not exist.

- [ ] **Step 3: Implement settings and database lifecycle**

Add settings:

```python
environment: Literal["fixture", "local", "production"] = "local"
repository_mode: Literal["memory", "postgres"] = "postgres"
auth_mode: Literal["local", "supabase"] = "local"
supabase_url: str = ""
supabase_anon_key: str = ""
redis_url: str = "redis://redis:6379/0"
local_user_id: UUID = UUID("00000000-0000-4000-8000-000000000023")
local_user_role: Literal["viewer", "editor", "admin"] = "admin"
```

Use `@model_validator(mode="after")` to reject local auth in production and
missing Supabase URL/key in production. `create_pool()` returns a
`psycopg_pool.ConnectionPool` with `min_size=1`, `max_size=10`,
`open=False`, and dict rows. FastAPI lifespan opens and closes it.

- [ ] **Step 4: Implement JWT verification and role dependencies**

Verify asymmetric Supabase JWTs against:

```python
jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
claims = jwt.decode(
    token,
    signing_key.key,
    algorithms=["RS256", "ES256"],
    options={"require": ["exp", "sub"]},
)
```

Do not use `user_metadata` for authorization. Resolve the role with
`repository.get_user_role(UUID(claims["sub"]))`. In local mode, use the
configured local identity and optional `X-Local-Role` only when environment is
not production.

- [ ] **Step 5: Move the memory repository and implement PostgreSQL job/profile methods**

Keep `apps/api/app/repository.py` as:

```python
from app.repositories.memory import InMemoryRepository
from app.repositories.postgres import PostgresRepository

__all__ = ["InMemoryRepository", "PostgresRepository"]
```

Implement SQL using parameter binding and one checked-out pooled connection per
repository call. Job transition must lock the row:

```sql
select * from import_jobs where id = %s for update;
```

Validate the transition before `update`. List queries use indexed filters,
`order by created_at desc`, and separate count queries. Profile CRUD uses the
existing `search_profiles` table.

- [ ] **Step 6: Implement PostgreSQL set reads and curated writes**

Return `SetDetail` by joining:

- `set_artists -> artists`;
- `set_events -> events`;
- `set_images -> images`;
- `field_candidates`.

Candidate acceptance updates `field_candidates.accepted` and moves an inbox set
to `reviewing`. Accepted artist candidates create/link `artists`; event, venue,
city, date, and year candidates update or create the linked `events` record.
`sets.title` remains the only direct curated text column on `sets`.

- [ ] **Step 7: Run unit and optional database integration tests**

Run:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_auth.py apps/api/tests/test_api.py -q
DATABASE_URL="${TEST_DATABASE_URL:-}" PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_postgres_repository.py -q
```

Expected: unit tests PASS. Integration tests PASS when `TEST_DATABASE_URL` is
set and otherwise report a single explicit skip.

- [ ] **Step 8: Commit**

```bash
git add apps/api/app/core apps/api/app/repositories apps/api/app/repository.py apps/api/app/routers apps/api/tests apps/api/requirements.txt
git commit -m "feat: add postgres repository and Supabase auth"
```

---

### Task 3: Celery Dispatch and Idempotent Processing Pipeline

**Files:**
- Create: `apps/api/app/services/import_pipeline.py`
- Create: `apps/api/app/workers/celery_app.py`
- Create: `apps/api/app/workers/dispatch.py`
- Create: `apps/api/app/workers/normalize_worker.py`
- Modify: `apps/api/app/core/config.py`
- Modify: `apps/api/app/repositories/base.py`
- Modify: `apps/api/app/repositories/memory.py`
- Modify: `apps/api/app/repositories/postgres.py`
- Modify: `apps/api/app/routers/imports.py`
- Modify: `apps/api/requirements.txt`
- Test: `apps/api/tests/test_jobs.py`

**Interfaces:**
- Consumes: `Repository`, `ImportJob`, `RawSetPayload`.
- Produces: `process_payload(repository, job_id, payload) -> UUID | None`.
- Produces: Celery task `app.workers.normalize_worker.process_raw_payload`.
- Produces: `JobDispatcher` used by import and profile routers.
- Produces: retry delays `(5, 30, 120)`.

- [ ] **Step 1: Pin Celery and Redis dependencies**

Add:

```text
celery[redis]==5.6.3
```

Install and verify:

```bash
.venv/bin/pip install -r apps/api/requirements.txt
.venv/bin/celery --version
```

- [ ] **Step 2: Write failing state and idempotency tests**

Add:

```python
def test_processing_same_payload_twice_returns_same_set(repository) -> None:
    first_job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    second_job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    first = process_payload(repository, first_job.id, PAYLOAD)
    second = process_payload(repository, second_job.id, PAYLOAD)
    assert first == second
    assert repository.get_job(second_job.id).details["duplicate"] is True


@pytest.mark.parametrize("attempt,delay", [(1, 5), (2, 30), (3, 120)])
def test_retry_delay(attempt: int, delay: int) -> None:
    assert retry_delay(attempt) == delay
```

Run and expect failure:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_jobs.py -q
```

- [ ] **Step 3: Implement the processing service**

`process_payload` must:

```python
score = calculate_set_score(payload.title, payload.duration_seconds or 0, repository.get_heuristic_config())
fingerprint = duplicate_fingerprint(payload.title, payload.duration_seconds or 0)
duplicate_id = repository.find_duplicate(payload, fingerprint)
if duplicate_id:
    repository.complete_duplicate_job(job_id, duplicate_id)
    return duplicate_id
if not score.accepted:
    repository.complete_discarded_job(job_id, score)
    return None
candidates = extract_field_candidates(payload.title, payload.description)
return repository.persist_processed_set(
    payload=payload,
    score=score,
    candidates=candidates,
    job_id=job_id,
    fingerprint=fingerprint,
)
```

Persist `score.reasons` and fingerprint inside `raw_payload`. Always create the
set with `review_status='inbox'`, including when `score.auto_accept` is true.

- [ ] **Step 4: Configure Celery**

Use:

```python
celery_app = Celery("syco23_setcrawler", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.workers.youtube_poller.*": {"queue": "youtube"},
        "app.workers.soundcloud_importer.*": {"queue": "soundcloud"},
        "app.workers.ftm_scraper.*": {"queue": "ftm"},
        "app.workers.normalize_worker.*": {"queue": "process"},
    },
)
```

Fixture tests set `task_always_eager=True` and
`task_eager_propagates=True`.

- [ ] **Step 5: Make API dispatch explicit and injectable**

Add `JobDispatcher` with:

```python
def dispatch_url(self, job: ImportJob) -> None: ...
def dispatch_profile(self, job: ImportJob) -> None: ...
def retry(self, job: ImportJob) -> None: ...
```

The production dispatcher calls `.delay(str(job.id))`. Tests inject a recording
dispatcher. `POST /imports/url` creates the durable row before dispatch.
Add `POST /imports/queue/{job_id}/retry` for admin users and reject jobs that
are not `failed` or `dead_letter`.

- [ ] **Step 6: Run tests**

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_jobs.py apps/api/tests/test_api.py -q
```

Expected: all job, idempotency, and existing API tests PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/services/import_pipeline.py apps/api/app/workers apps/api/app/routers/imports.py apps/api/app/repositories apps/api/tests apps/api/requirements.txt
git commit -m "feat: add durable Celery import pipeline"
```

---

### Task 4: YouTube Provider and Search Profiles

**Files:**
- Create: `apps/api/app/services/youtube.py`
- Create: `apps/api/app/workers/youtube_poller.py`
- Create: `apps/api/tests/fixtures/youtube_search.json`
- Create: `apps/api/tests/fixtures/youtube_videos.json`
- Create: `apps/api/tests/test_youtube.py`
- Modify: `apps/api/app/routers/search_profiles.py`
- Modify: `apps/api/app/schemas/profile.py`
- Modify: `apps/api/app/repositories/base.py`
- Modify: `apps/api/app/repositories/memory.py`
- Modify: `apps/api/app/repositories/postgres.py`
- Modify: `apps/api/app/services/normalizer.py`

**Interfaces:**
- Produces: `YouTubeAdapter.fetch(url: str) -> RawSetPayload`.
- Produces: `YouTubeAdapter.search(profile: SearchProfile) -> YouTubeSearchBatch`.
- Produces: task `run_youtube_profile(job_id: str)`.

- [ ] **Step 1: Write failing provider tests**

Tests assert:

```python
assert request.url.params["type"] == "video"
assert request.url.params["videoDuration"] == "long"
assert request.url.params["maxResults"] == "50"
assert len(videos_request.url.params["id"].split(",")) <= 50
assert batch.next_page_token == "NEXT_PAGE"
assert batch.payloads[0].duration_seconds == 5062
assert batch.payloads[0].primary_image_url.endswith("maxres.jpg")
```

Also test HTTP 403 with `reason="quotaExceeded"` raises
`ProviderQuotaError("youtube_quota_exceeded")`.

Run:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_youtube.py -q
```

Expected: FAIL because `YouTubeAdapter` does not exist.

- [ ] **Step 2: Implement the official API client**

Use `httpx.AsyncClient`, base URL `https://www.googleapis.com/youtube/v3`,
and server-only API key. Search parameters:

```python
{
    "part": "snippet",
    "type": "video",
    "videoDuration": "long",
    "maxResults": 50,
    "q": profile.query,
    "key": settings.youtube_api_key,
}
```

Include `pageToken` only when present. Request `videos.list` with
`part=snippet,contentDetails,status`. Batch IDs in slices of 50. Parse ISO-8601
durations without estimating. Preserve all thumbnail tiers in `raw_payload`.

- [ ] **Step 3: Implement direct video fetch and common normalization**

Accept only `youtube.com/watch?v=...` and `youtu.be/...`. `fetch` obtains the
video ID and calls the same detail method used by search. Missing/private videos
raise `ProviderPayloadError("youtube_video_unavailable")`.

- [ ] **Step 4: Implement the profile worker**

The task:

1. transitions the job to `processing`;
2. loads the referenced profile;
3. searches using the profile's `next_page_token`;
4. sends each payload to `process`;
5. stores the returned next token and `last_run_at`;
6. completes the job with result/discard/duplicate counts;
7. maps quota errors to `failed` without retry;
8. retries timeout, 429, and 5xx failures with the shared schedule.

- [ ] **Step 5: Expand profile API responses**

Expose `last_result_count`, `last_error_code`, and `latest_job_id` as derived
fields from the latest profile job; do not add redundant profile columns.
Profile creation/update/delete requires admin. Run-now requires admin.

- [ ] **Step 6: Run tests**

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_youtube.py apps/api/tests/test_api.py -q
```

Expected: YouTube fixture and profile API tests PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/services/youtube.py apps/api/app/workers/youtube_poller.py apps/api/app/routers/search_profiles.py apps/api/app/schemas apps/api/app/repositories apps/api/tests
git commit -m "feat: add YouTube metadata polling"
```

---

### Task 5: Isolated SoundCloud Metadata Import

**Files:**
- Create: `apps/api/app/services/soundcloud.py`
- Create: `apps/api/app/workers/soundcloud_importer.py`
- Create: `apps/api/tests/fixtures/soundcloud.json`
- Create: `apps/api/tests/test_soundcloud.py`
- Modify: `apps/api/app/routers/imports.py`
- Modify: `apps/api/app/services/normalizer.py`
- Modify: `docker/worker.Dockerfile`

**Interfaces:**
- Produces: `validate_soundcloud_url(url: str) -> str`.
- Produces: `SoundCloudAdapter.fetch(url: str) -> RawSetPayload`.
- Produces: task `import_soundcloud(job_id: str)`.

- [ ] **Step 1: Write failing URL and subprocess safety tests**

Test exact behavior:

```python
@pytest.mark.parametrize("url", [
    "http://soundcloud.com/crew/set",
    "https://evil.example/?next=https://soundcloud.com/crew/set",
    "https://soundcloud.com/",
    "https://soundcloud.com/crew/sets/playlist",
])
def test_rejects_unsafe_soundcloud_urls(url: str) -> None:
    with pytest.raises(ProviderValidationError):
        validate_soundcloud_url(url)


async def test_yt_dlp_never_uses_shell_or_download_flags(fake_process) -> None:
    await SoundCloudAdapter(process_runner=fake_process).fetch(VALID_URL)
    assert fake_process.shell is False
    assert "--skip-download" in fake_process.argv
    assert "--dump-single-json" in fake_process.argv
    assert "--no-playlist" in fake_process.argv
    assert "--ignore-config" in fake_process.argv
    assert "-o" not in fake_process.argv
```

Also test a timeout after 30 seconds and an output larger than the configured
1 MiB limit.

- [ ] **Step 2: Implement strict URL validation**

Parse with `urlsplit`. Require scheme `https`, hostname exactly
`soundcloud.com` or `www.soundcloud.com`, no credentials, and exactly two
non-empty path segments. Reject `/sets/`, `/likes`, `/reposts`, and query-based
redirect targets. Return a normalized URL without fragment.

- [ ] **Step 3: Implement bounded subprocess execution**

Run:

```python
argv = [
    settings.yt_dlp_bin,
    "--ignore-config",
    "--no-playlist",
    "--skip-download",
    "--dump-single-json",
    validated_url,
]
```

Use `asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)`, never
`create_subprocess_shell`. Enforce 30 seconds with `asyncio.timeout`, kill and
await on timeout, reject output beyond 1 MiB, parse one JSON object, and map
provider fields through `normalize_raw_payload`.

- [ ] **Step 4: Implement worker error mapping**

Invalid URLs and malformed output become `failed`. Timeouts and temporary
process failures follow the retry schedule. A valid payload is sent to the
common pipeline.

- [ ] **Step 5: Harden the worker image**

Install a pinned yt-dlp release in `docker/worker.Dockerfile`. Run as a
non-root user and rely on Compose for `read_only`, `tmpfs`, `cpus`, and
`mem_limit`. The worker command remains Celery; it must not invoke yt-dlp at
image startup.

- [ ] **Step 6: Run tests**

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_soundcloud.py apps/api/tests/test_jobs.py -q
```

Expected: all URL, argv, timeout, size, and normalization tests PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/services/soundcloud.py apps/api/app/workers/soundcloud_importer.py apps/api/app/routers/imports.py apps/api/app/services/normalizer.py apps/api/tests docker/worker.Dockerfile
git commit -m "feat: add isolated SoundCloud metadata imports"
```

---

### Task 6: Robots-Aware FreeTeknoMusic Adapter

**Files:**
- Create: `apps/api/app/services/ftm.py`
- Create: `apps/api/app/workers/ftm_scraper.py`
- Create: `apps/api/tests/fixtures/ftm_set.html`
- Create: `apps/api/tests/test_ftm.py`
- Modify: `apps/api/app/core/config.py`
- Modify: `apps/api/app/routers/imports.py`
- Modify: `apps/api/requirements.txt`

**Interfaces:**
- Produces: `FTMAdapter.fetch(url: str) -> RawSetPayload`.
- Produces: `FTMAdapter.crawl(start_url: str, max_pages: int) -> list[RawSetPayload]`.
- Produces: task `import_ftm(job_id: str)`.

- [ ] **Step 1: Pin parser dependency and write failing safety tests**

Add:

```text
beautifulsoup4==4.13.5
```

Tests:

```python
async def test_disabled_ftm_never_makes_http_request(recording_transport) -> None:
    adapter = FTMAdapter(enabled=False, transport=recording_transport)
    with pytest.raises(ProviderBlockedError, match="disabled"):
        await adapter.fetch(FTM_URL)
    assert recording_transport.requests == []


async def test_robots_denial_blocks_page_fetch(recording_transport) -> None:
    recording_transport.add("/robots.txt", "User-agent: *\nDisallow: /sets/")
    with pytest.raises(ProviderBlockedError, match="robots"):
        await enabled_adapter(recording_transport).fetch(f"{BASE}/sets/23hz")
    assert [request.url.path for request in recording_transport.requests] == ["/robots.txt"]
```

Add tests for unavailable robots, configured delay calls, maximum pages, URL
deduplication, and identical content hashes.

- [ ] **Step 2: Implement validation and robots checks**

Require HTTPS and hostname `freeteknomusic.org` or
`www.freeteknomusic.org`. Fetch `/robots.txt` using the configured User-Agent.
Parse with `urllib.robotparser.RobotFileParser`. A denied or unavailable robots
decision raises `ProviderBlockedError` before fetching content.

- [ ] **Step 3: Implement delayed metadata extraction**

Inject `sleep: Callable[[float], Awaitable[None]]` for testing. Wait
`scraper_request_delay_ms / 1000` between page requests. Enforce
`ftm_max_pages_per_run`, default 25.

Extract title from `og:title` then `<h1>`, description from
`og:description`, canonical URL from `<link rel="canonical">`, artwork from
`og:image`, and duration from structured metadata when present. Compute
`sha256(raw_html)` as `content_hash` and store raw HTML in `raw_payload`.
Never follow media/download links.

- [ ] **Step 4: Implement worker state mapping**

Disabled and robots-denied jobs become `blocked` with safe codes
`provider_disabled` and `robots_denied`. 429, timeout, and 5xx failures retry.
Valid payloads enter the common process queue.

- [ ] **Step 5: Run tests**

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_ftm.py apps/api/tests/test_jobs.py -q
```

Expected: all FTM safety and extraction tests PASS without live traffic.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/services/ftm.py apps/api/app/workers/ftm_scraper.py apps/api/app/core/config.py apps/api/app/routers/imports.py apps/api/tests apps/api/requirements.txt
git commit -m "feat: add robots-aware FTM metadata adapter"
```

---

### Task 7: Operational API, Provider Health, and Stats

**Files:**
- Create: `apps/api/app/routers/providers.py`
- Modify: `apps/api/app/routers/imports.py`
- Modify: `apps/api/app/routers/stats.py`
- Modify: `apps/api/app/main.py`
- Modify: `apps/api/app/repositories/base.py`
- Modify: `apps/api/app/repositories/memory.py`
- Modify: `apps/api/app/repositories/postgres.py`
- Modify: `apps/api/tests/test_api.py`
- Modify: `apps/api/tests/test_jobs.py`
- Create: `apps/api/tests/test_fixture_pipelines.py`

**Interfaces:**
- Produces: `GET /providers`.
- Produces: filtered `GET /imports/queue`.
- Produces: `POST /imports/queue/{job_id}/retry`.
- Produces: real queue counts in `GET /stats`.

- [ ] **Step 1: Write failing endpoint tests**

Add:

```python
def test_provider_health_redacts_secrets(admin_client) -> None:
    body = admin_client.get("/providers").json()
    assert body["youtube"]["configured"] is True
    assert "api_key" not in json.dumps(body).casefold()
    assert body["freeteknomusic"]["enabled"] is False


def test_queue_filters_by_source_and_status(editor_client) -> None:
    response = editor_client.get(
        "/imports/queue",
        params={"source": "soundcloud", "status": "failed"},
    )
    assert all(item["source"] == "soundcloud" for item in response.json()["items"])
    assert all(item["status"] == "failed" for item in response.json()["items"])
```

Run and expect failure:

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests/test_api.py apps/api/tests/test_jobs.py -q
```

- [ ] **Step 2: Implement provider health**

Return only booleans and safe operational values:

```json
{
  "youtube": {"configured": true, "enabled": true, "mode": "official_api"},
  "soundcloud": {"configured": true, "enabled": true, "mode": "manual_url"},
  "freeteknomusic": {"configured": true, "enabled": false, "mode": "robots_crawl"}
}
```

Never return keys, database URLs, subprocess paths, or contact email details.

- [ ] **Step 3: Implement queue pagination and retry**

`GET /imports/queue` accepts `source`, `status`, `limit` 1–100, and `offset`.
It returns `ImportJobPage`. Job detail is viewer-readable; job creation is
editor-only; terminal retry is admin-only.

- [ ] **Step 4: Replace synthetic stats**

`repository.stats()` counts persisted sets and jobs. Queue output includes all
states but the existing dashboard-compatible keys remain:

```python
"queue": {
    "queued": counts.get("queued", 0),
    "processing": counts.get("processing", 0),
    "failed": counts.get("failed", 0) + counts.get("dead_letter", 0),
    "completed": counts.get("completed", 0),
    "retry": counts.get("retry", 0),
    "blocked": counts.get("blocked", 0),
}
```

- [ ] **Step 5: Run complete API suite**

Add one parameterized offline acceptance test:

```python
@pytest.mark.parametrize("source", [
    SetSource.youtube,
    SetSource.soundcloud,
    SetSource.freeteknomusic,
])
def test_fixture_provider_reaches_review_inbox(source, fixture_dispatcher, repository) -> None:
    job = fixture_dispatcher.run_source(source)
    completed = repository.get_job(job.id)
    assert completed.status is JobStatus.completed
    created = repository.get_set(completed.result_set_id)
    assert created.review_status is ReviewStatus.inbox
```

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests -q
PYTHONPATH=apps/api .venv/bin/python -m compileall apps/api/app
```

Expected: all tests PASS and compileall reports no failures.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/routers apps/api/app/main.py apps/api/app/repositories apps/api/tests
git commit -m "feat: expose provider and import operations"
```

---

### Task 8: Nuxt Auth, Import Monitor, and Provider Operations

**Files:**
- Create: `apps/web/plugins/supabase.client.ts`
- Create: `apps/web/composables/useAuth.ts`
- Create: `apps/web/utils/auth.ts`
- Create: `apps/web/utils/jobs.ts`
- Create: `apps/web/components/JobStateBadge.vue`
- Create: `apps/web/components/ProviderHealth.vue`
- Create: `apps/web/pages/imports/index.vue`
- Create: `apps/web/pages/login.vue`
- Create: `apps/web/tests/unit/jobs.test.ts`
- Create: `apps/web/tests/unit/auth.test.ts`
- Modify: `apps/web/composables/useApi.ts`
- Modify: `apps/web/types/index.ts`
- Modify: `apps/web/data/demo.ts`
- Modify: `apps/web/data/navigation.ts`
- Modify: `apps/web/pages/index.vue`
- Modify: `apps/web/pages/import.vue`
- Modify: `apps/web/pages/search-profiles/index.vue`
- Modify: `apps/web/pages/inbox/index.vue`
- Modify: `apps/web/pages/inbox/[id].vue`
- Modify: `apps/web/pages/settings.vue`
- Modify: `apps/web/app.vue`
- Modify: `apps/web/assets/app.css`
- Modify: `apps/web/nuxt.config.ts`
- Modify: `apps/web/package.json`
- Modify: `apps/web/package-lock.json`
- Modify: `apps/web/tests/unit/contracts.test.ts`

**Interfaces:**
- Consumes: Task 7 operational endpoints.
- Produces: `useAuth()` with `user`, `role`, `canEdit`, `isAdmin`,
  `signInWithEmail`, and `signOut`.
- Produces: `useApi()` with authenticated `get` and `send`.
- Produces: `/imports` job monitor.

- [ ] **Step 1: Install and pin Supabase browser client**

Run:

```bash
cd apps/web
npm install --save-exact @supabase/supabase-js@2.111.0
```

Expected: package and lockfile contain the exact version.

- [ ] **Step 2: Write failing role and job UI tests**

Add pure capability helpers and test:

```typescript
expect(capabilities('viewer')).toEqual({ edit: false, admin: false })
expect(capabilities('editor')).toEqual({ edit: true, admin: false })
expect(capabilities('admin')).toEqual({ edit: true, admin: true })

expect(filterJobs(demoJobs, { source: 'soundcloud', status: 'failed' }))
  .toHaveLength(1)
expect(canRetry(demoJobs.find(job => job.status === 'dead_letter')!, 'admin'))
  .toBe(true)
expect(canRetry(demoJobs[0]!, 'editor')).toBe(false)
```

Update navigation expectation to include `Imports` after `Review Inbox`.

Run:

```bash
cd apps/web && npm test
```

Expected: FAIL because auth/job helpers and navigation do not exist.

- [ ] **Step 3: Implement Supabase session and local auth**

Create the client only when URL/key are configured. In production, obtain the
access token from `supabase.auth.getSession()`. In local mode send
`X-Local-Role` using the configured non-production role.

`useApi` builds:

```typescript
const headers = await authHeaders()
return await $fetch<T>(path, { baseURL: apiBase, headers, method, body, timeout })
```

Fallback demo data remains available only when
`NUXT_PUBLIC_RUNTIME_MODE=fixture`; operational local mode displays API errors
instead of silently pretending a write succeeded.

- [ ] **Step 4: Implement login and identity controls**

`/login` accepts email and calls:

```typescript
await supabase.auth.signInWithOtp({
  email,
  options: { emailRedirectTo: `${window.location.origin}/` }
})
```

The app shell displays the current role and sign-out action. Do not display or
store service-role credentials.

- [ ] **Step 5: Implement the import monitor**

`/imports`:

- fetches `/imports/queue?limit=50`;
- filters provider and state locally;
- refreshes every five seconds while any item is `queued`, `processing`, or
  `retry`;
- displays attempt count, error message, created time, and result set link;
- displays retry only for admin and terminal failure states.

Use `JobStateBadge` for every state, including `blocked` and `dead_letter`.

- [ ] **Step 6: Upgrade dashboard, import, profiles, and inbox**

- Dashboard renders `ProviderHealth` cards and real queue counts.
- Manual import shows returned job UUID and links to `/imports`.
- Search profiles support create, edit, enable/disable, delete, and run now.
- Inbox list shows duplicate/job context.
- Inbox detail displays score reasons and originating job.
- Candidate and status actions are hidden/disabled for viewers.
- Provider/profile configuration is admin-only.

- [ ] **Step 7: Preserve responsive SYCO23 styling**

Add plate-based job rows, state marks, mobile filter stacking, and bottom-nav
entry without neon, console, glossy, or cyberpunk treatment. Use existing color
tokens, hard borders, and rust-orange signal accents.

- [ ] **Step 8: Run frontend checks**

```bash
cd apps/web
npm test
npm run typecheck
npm run build
```

Expected: unit tests, Nuxt typecheck, and production build PASS.

- [ ] **Step 9: Commit**

```bash
git add apps/web
git commit -m "feat: add provider operations UI"
```

---

### Task 9: Docker Runtime and CI Integration

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker/api.Dockerfile`
- Modify: `docker/worker.Dockerfile`
- Modify: `.env.example`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Celery app/tasks from Tasks 3–6.
- Produces: one API, one web, PostgreSQL, Redis, and four queue worker services.
- Produces: CI with PostgreSQL/Redis integration.

- [ ] **Step 1: Write the Compose service contract check**

Run before editing:

```bash
docker compose config --services
```

Expected current output lacks `worker-youtube`, `worker-soundcloud`,
`worker-ftm`, and `worker-process`.

- [ ] **Step 2: Add worker services**

Every worker uses the same image and database/Redis environment. Commands:

```yaml
worker-youtube:
  command: celery -A app.workers.celery_app:celery_app worker -Q youtube --concurrency=1 --loglevel=INFO
worker-soundcloud:
  command: celery -A app.workers.celery_app:celery_app worker -Q soundcloud --concurrency=1 --loglevel=INFO
worker-ftm:
  command: celery -A app.workers.celery_app:celery_app worker -Q ftm --concurrency=1 --loglevel=INFO
worker-process:
  command: celery -A app.workers.celery_app:celery_app worker -Q process --concurrency=2 --loglevel=INFO
```

SoundCloud worker adds:

```yaml
read_only: true
tmpfs:
  - /tmp:size=64m,noexec,nosuid
cpus: 1.0
mem_limit: 512m
```

Mount `0001_init.sql`, `0003_indexes.sql`, and the generated provider-job
migration into plain PostgreSQL initialization in lexical order. The Supabase
RLS migration remains excluded from the Compose database because plain
PostgreSQL does not provide Supabase's `auth.uid()`, `anon`, and
`authenticated` runtime objects.

- [ ] **Step 3: Complete environment defaults**

Add:

```dotenv
ENVIRONMENT=local
REPOSITORY_MODE=postgres
AUTH_MODE=local
LOCAL_USER_ROLE=admin
SUPABASE_URL=
SUPABASE_ANON_KEY=
REDIS_URL=redis://redis:6379/0
PROVIDER_MODE=fixture
PROVIDER_REQUEST_TIMEOUT_SECONDS=20
PROVIDER_OUTPUT_LIMIT_BYTES=1048576
FTM_MAX_PAGES_PER_RUN=25
NUXT_PUBLIC_RUNTIME_MODE=local
NUXT_PUBLIC_LOCAL_ROLE=admin
```

Retain all existing variables and server-only secret boundaries.

- [ ] **Step 4: Add CI services and checks**

API CI starts PostgreSQL 16 and Redis 7 services. Before applying every
migration, it creates non-login `anon` and `authenticated` roles plus a
test-only `auth.uid()` compatibility function. It then applies migrations with
`psql`, sets `TEST_DATABASE_URL`, and runs all tests and compileall. Web CI
runs `npm ci`, tests, typecheck, and build.

- [ ] **Step 5: Validate infrastructure**

Run:

```bash
docker compose config
docker compose config --services
```

Expected services include:

```text
web
api
db
redis
worker-youtube
worker-soundcloud
worker-ftm
worker-process
```

If a Docker engine is available:

```bash
docker compose up -d --build
docker compose ps
curl --fail http://localhost:8000/health
docker compose down
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml docker .env.example .github/workflows/ci.yml
git commit -m "chore: wire provider worker runtime"
```

---

### Task 10: Documentation, Full Verification, and Release Archive

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/superpowers/plans/2026-07-28-setcrawler-v0.2-full-provider-pack.md`
- Create: `syco23-setcrawler-local.zip`

**Interfaces:**
- Produces: complete local v0.2 handoff.

- [x] **Step 1: Update documentation**

README must document:

- v0.2 feature list and non-goals;
- native and Docker startup;
- Supabase project/JWKS setup;
- role provisioning;
- YouTube key setup;
- SoundCloud metadata-only security;
- FTM opt-in and robots behavior;
- fixture-mode verification;
- migration order;
- all test commands.

Architecture must show Nuxt → FastAPI → PostgreSQL/Redis → provider/process
workers, the job-state model, auth boundary, idempotency keys, and explicit
non-publication invariant.

> Final-release runtime correction (2026-07-29): provider workers now enqueue
> `RawSetPayload` plus the exact durable claim token to the `process` queue;
> they do not execute the common pipeline inline. Worker-loss rejection,
> late acknowledgements, lease-expiry redispatch, and safe API dispatch
> terminalization close the persisted-job recovery paths. YouTube profiles use
> durable child jobs plus a fenced finalizer. Search-profile deletion is an
> admin-only soft delete that conflicts with active jobs and retains terminal
> job identity. FTM pacing applies between every HTTP request.

- [x] **Step 2: Run the complete API verification**

```bash
PYTHONPATH=apps/api .venv/bin/pytest apps/api/tests -q
PYTHONPATH=apps/api .venv/bin/python -m compileall apps/api/app
```

Expected: all tests PASS and compileall has zero failures.

- [x] **Step 3: Run the complete web verification**

```bash
cd apps/web
npm test
npm run typecheck
npm run build
```

Expected: all tests and builds PASS.

- [ ] **Step 4: Validate migrations and Compose**

```bash
rg -n "create table import_jobs|create table provider_cursors|enable row level security|grant .*authenticated" supabase/migrations
docker compose config
```

Expected: all schema/security markers found and Compose configuration valid.

> Evidence (2026-07-29): the migration marker and static YAML service audit
> passed, but this checkbox remains open because the required `docker compose
> config` render could not execute without a Docker CLI/runtime.

- [x] **Static substitute: parse Compose YAML and assert required services**

This checks YAML syntax and the expected services only; it does not validate
Compose interpolation, mounts, resource keys, or rendered configuration.

- [ ] **Step 5: Run browser smoke test when available**

```bash
cd apps/web
npx playwright test
```

Expected: PASS. If the environment lacks a browser, record the exact Playwright
availability error without weakening unit/type/build acceptance.

> Evidence (2026-07-29): the checked-in web-server configuration now starts
> API and Nuxt in fixture/memory/local mode, so no PostgreSQL or live provider
> is required. The smoke run reached Chromium launch and stopped only because
> `/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome` is absent.
> The checkbox remains open; no browser was downloaded.

- [x] **Step 6: Package a clean archive**

Create the archive excluding generated and secret material:

```bash
zip -r syco23-setcrawler-local.zip \
  . \
  -x '.git/*' '.venv/*' 'apps/web/node_modules/*' 'apps/web/.nuxt/*' \
     'apps/web/.output/*' '**/__pycache__/*' '**/.pytest_cache/*' \
     '.env' 'syco23-setcrawler-local.zip'
unzip -t syco23-setcrawler-local.zip
```

Expected: archive integrity reports no errors.

> Evidence (2026-07-29): `unzip -t` reported no errors. The archive audit
> excluded local environments, dependency/build/test outputs, `.superpowers`,
> `supabase/.temp`, restored archives and actual `.env` files while retaining
> `.env.example` and the approved plan/specification.

- [ ] **Step 7: Commit**

```bash
git add README.md docs
git commit -m "docs: document full provider pack"
```

---

## Current Documentation References

- [Supabase JWT verification](https://supabase.com/docs/guides/auth/jwts)
- [Supabase PostgreSQL connections and pooling](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase changelog](https://supabase.com/changelog)
- [YouTube `search.list`](https://developers.google.com/youtube/v3/docs/search/list)
- [YouTube `videos.list`](https://developers.google.com/youtube/v3/docs/videos/list)
- [Celery task retries](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Celery Redis broker/backend](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
- [yt-dlp metadata options](https://github.com/yt-dlp/yt-dlp#video-selection)
