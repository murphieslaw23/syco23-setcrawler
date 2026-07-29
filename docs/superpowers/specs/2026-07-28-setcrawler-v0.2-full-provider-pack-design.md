# SYCO23 SETCRAWLER v0.2 — Full Provider Pack Design

**Date:** 2026-07-28  
**Status:** Approved design, awaiting final specification review  
**Project:** `syco23-setcrawler`  
**Brand:** SYSTEM CORRUPT / SYCO23

## 1. Goal

Version 0.2 turns the Phase 1 local review application into an operational,
queue-driven metadata ingestion system for YouTube, SoundCloud, and
freeteknomusic.org.

The release must:

- persist application state in PostgreSQL;
- authenticate users through Supabase Auth and enforce viewer, editor, and
  admin roles;
- process provider work through Redis and Celery;
- normalize all providers into the existing set schema;
- expose durable import-job state through the API and Nuxt UI;
- preserve the human review workflow;
- never download audio or video;
- never publish a set automatically.

Image processing, OCR, and perceptual image hashing remain outside v0.2.
Provider thumbnails are retained as remote metadata only.

## 2. Architecture

### 2.1 Runtime topology

The Nuxt application calls the FastAPI REST API. FastAPI authenticates requests,
persists application records in PostgreSQL, and dispatches background work to
Celery through Redis. Celery workers are separated by provider and processing
responsibility.

Queues:

- `youtube`: search-profile runs and YouTube metadata fetching;
- `soundcloud`: manual URL imports through the isolated yt-dlp runner;
- `ftm`: conservative freeteknomusic.org crawling;
- `process`: normalization, deduplication, scoring, enrichment, and persistence.

The existing in-memory repository remains available only for isolated unit tests
and explicit offline demo mode. Normal local and production execution uses a
`PostgresRepository`.

### 2.2 Provider contract

Every direct URL provider adapter implements:

```python
async def fetch(url: str) -> RawSetPayload:
    ...
```

YouTube additionally implements search-profile execution because profile search
is not representable as a single provider URL. Search results are converted to
the same `RawSetPayload` contract before entering the processing pipeline.

### 2.3 Persistence additions

The release retains the existing tables and adds only the infrastructure needed
for durable jobs and cursors:

#### `import_jobs`

- UUID primary key;
- provider/source;
- job type;
- input URL or search-profile reference;
- state: `queued`, `processing`, `retry`, `completed`, `failed`, `blocked`,
  or `dead_letter`;
- attempt counter;
- timestamps for creation, start, finish, and next retry;
- resulting set reference when available;
- safe error code and message;
- structured job details.

#### `provider_cursors`

- provider and logical cursor key;
- cursor/token value;
- last successful use timestamp;
- update timestamp;
- unique provider/key pair.

YouTube profile page tokens may continue to use `search_profiles.next_page_token`.
`provider_cursors` exists for provider-wide crawl state and future cursor types
without embedding them in job rows.

### 2.4 Authentication and authorization

Supabase JWTs are verified by FastAPI. Roles are resolved from `user_roles`.

| Role | Permissions |
| --- | --- |
| Viewer | Read published and otherwise permitted content |
| Editor | Read and curate the inbox, decide candidates, start URL imports |
| Admin | Manage providers and search profiles, enable FTM, retry terminal jobs |

`AUTH_MODE=local` may provide a documented local test identity. Application
startup must reject this mode when the environment is marked as production.

## 3. Import Data Flow

1. FastAPI validates the request and the caller's role.
2. The API creates an `import_jobs` row before dispatching background work.
3. Celery receives the durable job UUID, not the complete mutable job payload.
4. A provider worker loads the job and fetches metadata.
5. The worker emits a `RawSetPayload` to the common processing queue.
6. The processor normalizes the payload.
7. Duplicate checks run in the following order:
   - source plus source ID;
   - normalized canonical URL;
   - fingerprint from normalized title and duration.
8. The heuristic scorer applies the configurable duration and keyword rules.
9. Rejected low-confidence data is logged without creating an inbox entry.
10. Passing data is enriched into field candidates.
11. Set, candidates, job result, and import audit records are persisted in one
    transactional boundary where practical.
12. The resulting set is always reviewable and is never auto-published.

Worker tasks must be idempotent. Re-running the same job must not create a
second set.

## 4. Provider Design and Safety

### 4.1 YouTube

YouTube uses the official Data API:

- `search.list` with `type=video` and `videoDuration=long`;
- configurable search profiles;
- at most 50 results per page;
- persisted page tokens;
- batched `videos.list` requests for full snippets and content details;
- optional batched `channels.list` requests for fallback channel artwork;
- safe handling for quota exhaustion, missing videos, disabled videos, and
  partial batch results.

Quota usage is observed and reported but old fixed quota arithmetic is not
embedded as a correctness rule. CI uses fixtures and never spends live quota.

### 4.2 SoundCloud

SoundCloud is manual URL import only.

Accepted inputs:

- HTTPS;
- host exactly `soundcloud.com` or an explicitly approved canonical subdomain;
- a single track/set URL;
- no playlist expansion.

yt-dlp is executed using an argument array without a shell. Required behavior:

- `--ignore-config`;
- `--no-playlist`;
- `--skip-download`;
- `--dump-single-json`;
- 30-second process timeout;
- bounded standard output and error capture;
- no persistent output files.

The production runner uses a dedicated container with one CPU, 512 MB memory,
tmpfs-only writable space, and the narrowest practical network access.

### 4.3 freeteknomusic.org

FTM crawling is disabled by default and requires
`FTM_SCRAPER_ENABLED=true`.

Each crawl:

- loads and evaluates `robots.txt` before fetching target pages;
- stops with `blocked` if permission is denied or cannot be established
  safely;
- sends the configured identifying User-Agent;
- waits the configured 5–10 seconds between requests;
- enforces a maximum number of pages per run;
- deduplicates URLs and content hashes;
- stores raw HTML in `raw_payload` for later reparsing;
- does not fetch or store audio files.

## 5. Job State and Failure Handling

Normal flow:

```text
queued -> processing -> completed
```

Alternative terminal and retry states:

- `retry`: timeouts, HTTP 429, and temporary provider/server failures;
- `failed`: invalid input, missing credentials, or malformed provider data;
- `blocked`: disabled provider or robots exclusion;
- `dead_letter`: retry budget exhausted.

Retry delays are 5, 30, and 120 seconds with a maximum of three attempts.
Validation errors and robots exclusions are not retried.

API error output must be useful to an operator without exposing credentials,
provider response bodies containing sensitive data, or subprocess internals.

## 6. User Interface

### 6.1 Dashboard

The dashboard shows:

- health/configuration state for all providers;
- counts for queued, running, completed, and failed jobs;
- recent import runs;
- quota and rate-limit warnings;
- a direct SoundCloud URL import action.

### 6.2 Import monitor

A new `/imports` page provides:

- provider and state filters;
- automatic refresh for active jobs;
- progress, attempt count, and safe failure messages;
- a link to the generated set;
- terminal-job retry for authorized users.

### 6.3 Search profiles

`/search-profiles` supports:

- create, edit, disable, and delete;
- run now;
- last-run time and result count;
- pagination state;
- profile-level configuration and quota errors.

### 6.4 Review inbox

Inbox views add:

- originating job and provider;
- duplicate warnings;
- score breakdown;
- extracted artist, event, year, date, venue, and city candidates;
- independent candidate accept/reject controls;
- explicit set acceptance and rejection;
- no automatic publication path.

### 6.5 Visual constraints

The existing SYCO23 visual system remains intact:

- dark mechanical plates and stacked forms;
- rust orange as the dominant signal color;
- clear state signals rather than decorative glitch;
- mobile bottom-tab navigation;
- no terminal, neon, cyberpunk, glossy, or vinyl-player metaphors.

No new generated imagery or complete redesign is required for v0.2.

## 7. Operating Modes

| Mode | Purpose | Provider behavior |
| --- | --- | --- |
| `fixture` | Deterministic tests | No live provider calls |
| `local` | Local development | PostgreSQL/Redis; live providers only when explicitly configured |
| `production` | Deployed runtime | Auth mandatory; local auth prohibited |

FTM requires its independent enable switch in every mode capable of live
network access.

## 8. Testing Strategy

### 8.1 API and processing

Tests cover:

- heuristic duration and keyword behavior;
- normalizers for all providers;
- artist, event, date, year, venue, and city extraction;
- all duplicate keys;
- repository contract for memory and PostgreSQL implementations;
- role enforcement;
- job transitions, retries, and dead-letter behavior.

### 8.2 Providers

All routine CI provider tests use recorded fixtures or fake HTTP transports:

- YouTube pagination, batching, partial results, and quota failure;
- exact yt-dlp argument construction, timeout, output bounds, and URL rejection;
- FTM robots denial, kill switch, delay control, page limit, and hash
  deduplication.

### 8.3 Infrastructure

- Celery uses eager mode for focused unit tests.
- CI starts PostgreSQL and Redis service containers for integration tests.
- Migrations are applied to an empty database.
- A duplicate job execution proves idempotency.

### 8.4 Frontend

Vitest covers:

- job rendering and filters;
- role-dependent controls;
- candidate decisions;
- failure and retry states;
- search-profile management.

Nuxt typecheck and production build are mandatory. A browser smoke test runs
when a compatible browser is available.

## 9. Acceptance Criteria

Version 0.2 is complete when:

1. Docker Compose starts Nuxt, FastAPI, PostgreSQL, Redis, and Celery workers.
2. All migrations apply to an empty database.
3. Fixture pipelines for all three providers finish in the review inbox.
4. A configured YouTube profile can be run manually against the live API.
5. A valid SoundCloud URL can be processed as metadata without media download.
6. FTM remains disabled by default and respects robots rules when enabled.
7. Import jobs are observable through API and UI.
8. Authentication and role boundaries are enforced.
9. No workflow automatically publishes a set.
10. API tests, web tests, typecheck, and production build pass.
11. README, architecture, setup, and operating documentation are current.
12. The complete local project is packaged as a downloadable archive.

## 10. Explicit Non-goals

The following remain for later releases:

- OCR;
- flyer classification;
- image downloading and Supabase Storage ingestion;
- perceptual image hashing;
- automated SoundCloud search;
- automatic publication;
- production deployment itself.

