# SYCO23 Setcrawler Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a locally runnable SYCO23 Setcrawler Phase 1 review application with tested FastAPI contracts, a responsive Nuxt editorial UI, and production-oriented local infrastructure.

**Architecture:** FastAPI owns typed application contracts and a repository abstraction with deterministic local seed data. Nuxt consumes the API through one composable and renders a dense responsive review workflow. Postgres/Supabase migration files remain the source-of-truth deployment schema, while local demo mode keeps first-run setup friction low.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, pytest, Nuxt 3, Vue 3, TypeScript, Vitest, Docker Compose, Postgres 16, Redis 7.

## Global Constraints

- Brand names are SYSTEM CORRUPT, SYCO23, and SYCO only.
- Never download copyrighted audio or video.
- Never auto-publish a set; every record enters a reviewable state.
- Phase 1 provider traffic is disabled; use deterministic local data.
- Supported sources are `youtube`, `soundcloud`, and `freeteknomusic`.
- Review states are `inbox`, `reviewing`, `accepted`, `rejected`, and `published`.
- UI uses rust orange `#b54d18` on dark surface `#171311`.
- No terminal/console, neon, cyberpunk, glossy startup, soft skeuomorphic, or vinyl metaphors.

---

### Task 1: Domain Logic and API Contracts

**Files:**
- Create: `apps/api/tests/test_heuristic.py`
- Create: `apps/api/tests/test_extraction.py`
- Create: `apps/api/tests/test_api.py`
- Create: `apps/api/app/services/heuristic.py`
- Create: `apps/api/app/services/enricher.py`
- Create: `apps/api/app/schemas/*.py`
- Create: `apps/api/app/repository.py`
- Create: `apps/api/app/main.py`
- Create: `apps/api/app/routers/*.py`

**Interfaces:**
- Produces: `calculate_set_score(title: str, duration_seconds: int, config: HeuristicConfig) -> ScoreResult`
- Produces: `extract_field_candidates(title: str, description: str | None) -> list[FieldCandidateCreate]`
- Produces: REST contracts under `/health`, `/sets`, `/imports`, `/search-profiles`, `/stats`.

- [ ] Write unit and API tests that fail because the modules do not exist.
- [ ] Run `pytest apps/api/tests -q` and verify collection or assertion failures.
- [ ] Implement the smallest typed domain and repository surface that satisfies the tests.
- [ ] Run `pytest apps/api/tests -q` and verify all tests pass.

### Task 2: Nuxt Review Experience

**Files:**
- Create: `apps/web/nuxt.config.ts`
- Create: `apps/web/app.vue`
- Create: `apps/web/assets/app.css`
- Create: `apps/web/composables/useApi.ts`
- Create: `apps/web/data/demo.ts`
- Create: `apps/web/components/*.vue`
- Create: `apps/web/pages/**/*.vue`

**Interfaces:**
- Consumes: `GET /stats`, `GET /sets`, `GET /sets/{id}`, candidate actions, review actions, imports, and search profiles.
- Produces: responsive dashboard, inbox, detail review flow, directories, settings, and URL import interaction.

- [ ] Add a Vitest contract test for source labels, score formatting, and supported navigation.
- [ ] Run `npm test` in `apps/web` and verify the missing implementation fails.
- [ ] Implement the app shell, shared components, pages, demo fallback, and real API actions.
- [ ] Run `npm test` and `npm run build` in `apps/web`.

### Task 3: Persistence and Local Runtime

**Files:**
- Create: `supabase/migrations/0001_init.sql`
- Create: `supabase/migrations/0002_rls.sql`
- Create: `supabase/migrations/0003_indexes.sql`
- Create: `docker/api.Dockerfile`
- Create: `docker/worker.Dockerfile`
- Create: `docker-compose.yml`
- Create: `.env.example`

**Interfaces:**
- Produces: Compose services `web`, `api`, `db`, and `redis`.
- Produces: Supabase-compatible schema and RLS policies.

- [ ] Add configuration validation checks to the API test suite.
- [ ] Implement migration and runtime files using only documented environment variables.
- [ ] Run `docker compose config` and inspect the resolved service graph.

### Task 4: Documentation and CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `README.md`
- Create: `docs/architecture.md`
- Create: `scripts/init-git.sh`
- Create: `.gitignore`

**Interfaces:**
- Produces: exact local start, test, Docker, Supabase, and deployment instructions.

- [ ] Document demo mode and credentialed Postgres/Supabase mode.
- [ ] Add API compile/test and web test/build CI jobs.
- [ ] Validate paths and commands against the actual repository.

### Task 5: End-to-End Verification and Artifact

**Files:**
- Create: `apps/web/tests/e2e/review-flow.spec.ts`
- Create: `playwright.config.ts`
- Create: `syco23-setcrawler-local.zip`

**Interfaces:**
- Consumes: running API and Nuxt app.
- Produces: fresh test/build/browser evidence and a reusable archive.

- [ ] Run all API tests and Python compilation.
- [ ] Run web tests and the production Nuxt build.
- [ ] Start the local services, inspect desktop and mobile screenshots, and exercise inbox-to-detail review actions.
- [ ] Compare the implementation screenshot with the accepted concept on at least five visual criteria.
- [ ] Package the source without build caches, secrets, or generated dependencies.
