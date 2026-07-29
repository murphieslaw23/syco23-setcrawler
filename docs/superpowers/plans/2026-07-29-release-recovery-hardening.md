# SETCRAWLER v0.2 Release Recovery Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make durable imports recover correctly after early delivery, worker loss, or Redis data loss, then produce a verified v0.2 release candidate and synchronize the remaining roadmap.

**Architecture:** PostgreSQL remains the source of truth for import state. Repository claims enforce both lease expiry and `next_retry_at`; a periodic Celery redriver republishes only queued, due-retry, or stale-processing jobs; durable retry counters prevent Celery message recreation from resetting the three-failure budget. Redis uses AOF persistence locally, while the database redriver remains the recovery authority.

**Tech Stack:** Python 3.12, FastAPI, Celery, Redis 7, PostgreSQL/Supabase migrations, pytest, Nuxt 3, Vitest, Docker Compose, GitHub Actions.

## Global Constraints

- Never download provider audio or video; process metadata and thumbnails only.
- Never auto-publish a set; successful imports enter the editorial inbox.
- PostgreSQL is authoritative for job state; Redis is a transport, not the source of truth.
- Retry policy remains three processing failures with delays of 5, 30, and 120 seconds.
- Existing API routes and Pydantic response contracts remain backward compatible.
- Supabase service-role credentials remain server-only.
- The public brand remains SYCO23; SYSTEM CORRUPT is used only for formal brand context.

---

### Task 1: Enforce durable retry availability

**Files:**
- Modify: `apps/api/app/repositories/base.py`
- Modify: `apps/api/app/repositories/memory.py`
- Modify: `apps/api/app/repositories/postgres.py`
- Modify: `apps/api/app/workers/recovery.py`
- Test: `apps/api/tests/test_release_recovery.py`

**Interfaces:**
- Consumes: `ImportJob.next_retry_at`, `Settings.job_claim_ttl_seconds`
- Produces: `Repository.list_recoverable_jobs(claim_ttl_seconds: int, limit: int) -> list[ImportJob]`

- [ ] **Step 1: Write the failing retry-time tests**

```python
def test_future_retry_cannot_be_claimed_or_redriven() -> None:
    repository = InMemoryRepository()
    job = _retry_job(repository, next_retry_at=datetime.now(UTC) + timedelta(minutes=5))
    assert repository.claim_job(job.id) is None
    assert repository.list_recoverable_jobs(claim_ttl_seconds=300, limit=50) == []


def test_due_retry_and_stale_processing_are_recoverable() -> None:
    repository = InMemoryRepository()
    due = _retry_job(repository, next_retry_at=datetime.now(UTC) - timedelta(seconds=1))
    stale = _stale_processing_job(repository, age_seconds=301)
    assert {job.id for job in repository.list_recoverable_jobs(
        claim_ttl_seconds=300,
        limit=50,
    )} == {due.id, stale.id}
```

- [ ] **Step 2: Run the focused tests and confirm the gap**

Run: `cd apps/api && PYTHONPATH=. pytest tests/test_release_recovery.py -q`

Expected: FAIL because future retry rows are claimable and `list_recoverable_jobs` does not exist.

- [ ] **Step 3: Gate claims and expose recoverable rows**

Implement the same predicates in memory and PostgreSQL:

```text
status = queued
OR status = retry AND (next_retry_at IS NULL OR next_retry_at <= now)
OR status = processing AND started_at < now - claim_ttl
```

When `claim_or_reschedule` sees a future retry, publish one replacement with a countdown equal to the remaining durable delay and return without claiming.

- [ ] **Step 4: Prove memory and PostgreSQL semantics**

Run: `cd apps/api && PYTHONPATH=. pytest tests/test_release_recovery.py tests/test_postgres_repository.py -q`

Expected: PASS, including the real PostgreSQL integration test when `TEST_DATABASE_URL` is configured.

### Task 2: Preserve retry budget across message recreation

**Files:**
- Modify: `apps/api/app/workers/normalize_worker.py`
- Test: `apps/api/tests/test_release_recovery.py`

**Interfaces:**
- Consumes: existing `_record_retry(..., retries: int, ...)`
- Produces: durable `details.retry_count` used as the authoritative lower bound

- [ ] **Step 1: Write the failing durable-budget test**

```python
def test_durable_retry_count_survives_celery_retry_reset() -> None:
    repository, job, claim = _processing_job()
    assert _record_retry(repository, job.id, RuntimeError("one"), 0,
                         claim_started_at=claim.started_at) == 5
    reclaimed = repository.claim_job(job.id)
    assert reclaimed is not None
    assert _record_retry(repository, job.id, RuntimeError("two"), 0,
                         claim_started_at=reclaimed.started_at) == 30
    assert repository.get_job(job.id).details["retry_count"] == 2
```

- [ ] **Step 2: Run the focused test and confirm the reset**

Run: `cd apps/api && PYTHONPATH=. pytest tests/test_release_recovery.py::test_durable_retry_count_survives_celery_retry_reset -q`

Expected: FAIL because both calls currently return the first delay.

- [ ] **Step 3: Persist the failure count atomically with retry state**

Compute `failure_count = max(self.request.retries, details.retry_count) + 1`, use it to select 5/30/120 seconds, and store it in the same `ImportJobPatch.details` update as `status=retry`. On the fourth failure, transition to `dead_letter`.

- [ ] **Step 4: Verify retry and dead-letter behavior**

Run: `cd apps/api && PYTHONPATH=. pytest tests/test_release_recovery.py tests/test_final_release_fixes.py -q`

Expected: PASS with durable delays `5`, `30`, `120`, followed by `dead_letter`.

### Task 3: Add database-backed redrive and persistent Redis

**Files:**
- Modify: `apps/api/app/workers/normalize_worker.py`
- Modify: `apps/api/app/workers/celery_app.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Test: `apps/api/tests/test_release_recovery.py`
- Test: `apps/api/tests/test_infrastructure_contract.py`

**Interfaces:**
- Consumes: `Repository.list_recoverable_jobs(...)`, `JobDispatcher.retry(job)`
- Produces: Celery task `app.workers.normalize_worker.redrive_import_jobs`

- [ ] **Step 1: Write failing redrive and infrastructure tests**

```python
def test_redriver_dispatches_each_recoverable_job_once(monkeypatch) -> None:
    repository = InMemoryRepository()
    queued = repository.create_job(
        url="https://soundcloud.com/syco23/redrive",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    calls: list[UUID] = []
    monkeypatch.setattr(normalize_worker, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(normalize_worker, "JobDispatcher",
                        lambda: SimpleNamespace(retry=lambda job: calls.append(job.id)))
    assert normalize_worker.redrive_import_jobs.run() == 1
    assert calls == [queued.id]
```

Also assert Compose contains `--appendonly yes`, a `redis_data` volume, and a Celery beat service.

- [ ] **Step 2: Run focused tests and confirm missing recovery services**

Run: `cd apps/api && PYTHONPATH=. pytest tests/test_release_recovery.py tests/test_infrastructure_contract.py -q`

Expected: FAIL because the task, beat service, and persistent Redis configuration are absent.

- [ ] **Step 3: Implement redrive**

Add a boundless periodic task that reads at most `JOB_REDRIVE_BATCH_SIZE` recoverable rows, dispatches each through `JobDispatcher.retry`, continues after per-job publish failures, and returns the number successfully published. Schedule it every `JOB_REDRIVE_INTERVAL_SECONDS`.

- [ ] **Step 4: Harden the local runtime**

Configure Redis with AOF and a named data volume. Add a `worker-beat` container using the same API image and environment as workers. Keep database claim fencing as the protection against duplicate broker deliveries.

- [ ] **Step 5: Verify recovery contracts**

Run: `cd apps/api && PYTHONPATH=. pytest tests/test_release_recovery.py tests/test_infrastructure_contract.py -q`

Expected: PASS.

### Task 4: Build the release candidate

**Files:**
- Modify: `README.md`
- Create: `docs/releases/2026-07-29-v0.2-release-candidate.md`

**Interfaces:**
- Consumes: verified API, web, migration, and Compose commands
- Produces: reproducible release evidence and documented operational limits

- [ ] **Step 1: Run the complete API suite**

Run: `cd apps/api && PYTHONPATH=. pytest tests -q`

Expected: all tests pass, with PostgreSQL-only tests skipped only when no test database is available.

- [ ] **Step 2: Run static API validation**

Run: `cd apps/api && python -m compileall app`

Expected: exit code 0.

- [ ] **Step 3: Run complete frontend gates**

Run: `cd apps/web && npm ci && npm test && npm run typecheck && npm run build`

Expected: all commands exit 0 and Nuxt emits a production build.

- [ ] **Step 4: Validate Docker Compose**

Run: `docker compose config --quiet`

Expected: exit code 0.

- [ ] **Step 5: Record exact release evidence**

Document command results, remaining live-integration requirements, rollback notes, and the fact that no production deployment is implied by a successful local release build.

### Task 5: Synchronize roadmap and release state

**Files:**
- Modify: `docs/superpowers/plans/2026-07-29-release-recovery-hardening.md`
- Create or modify: GitHub issues in `murphieslaw23/syco23-setcrawler`
- Create: Notion page `SYCO23 SETCRAWLER — v0.2 Release & Development Plan`

**Interfaces:**
- Consumes: final release evidence and unresolved requirements
- Produces: GitHub issues with acceptance criteria and a Notion plan linking repository, commit, and issues

- [ ] **Step 1: Publish the verified release change**

Create a release branch and commit with conventional message `fix: harden durable import recovery`, then open a pull request to `main`.

- [ ] **Step 2: Create only genuinely open GitHub issues**

Create separate issues for live Supabase provisioning/migration verification, Vercel frontend project linkage, production API/worker hosting, provider credential smoke tests, storage buckets/image pipeline, OCR, and production observability. Each issue must include acceptance criteria and dependencies.

- [ ] **Step 3: Mirror the plan to Notion**

Create the release/dev-plan page under the existing `SYCO23 — Project Hub`, preserving the hub’s existing content. Include GitHub issue links, current release evidence, deployment gates, and owner-facing next actions.

- [ ] **Step 4: Verify external synchronization**

Fetch the GitHub PR/issues and the Notion page after creation. Check the Vercel team for a SETCRAWLER project; if none exists, leave deployment unclaimed and keep the Vercel-linkage issue open.

