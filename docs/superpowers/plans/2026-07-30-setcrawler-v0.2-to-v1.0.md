# SYCO23 SETCRAWLER v0.2 to v1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the deployed metadata-only SETCRAWLER v0.2 into a production-grade v1.0 liveset preservation platform with extensible providers, canonical cross-provider sets, manual merge review, rights-aware audio quarantine, MinIO storage, MP3 streaming, and a public archive.

**Architecture:** Keep FastAPI, PostgreSQL/Supabase, Redis/Celery, Nuxt, and the existing durable job model. Replace the closed provider enum and one-source-per-set model with a capability registry plus canonical sets linked to provider items. Add a separate rights and audio domain whose only acquisition paths are official provider downloads, direct creator uploads, or explicit administrator evidence; store private audio in MinIO and expose only policy-checked streaming or downloads.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, psycopg 3, Celery 5.6, Redis 7, PostgreSQL/Supabase, MinIO, FFmpeg/ffprobe, feedparser, mutagen, Nuxt 3, Vue 3, TypeScript, Vitest, Playwright, pytest.

## Global constraints

- Keep the no-auto-publish invariant.
- Never scrape, extract, or rip audio from a provider stream.
- Acquire audio only through documented provider downloads, direct creator uploads, or explicit rights evidence.
- YouTube is metadata/embed only. Mixcloud is metadata/embed/syndication only.
- Public streaming requires approved rights; downloads additionally require `allow_download=true`.
- Authorized files enter private quarantine immediately and expire after 30 days unless approved.
- Preserve valuable originals; keep good MP3s; otherwise create a 256 kbit/s CBR MP3 derivative.
- Serve approved MP3 through HTTP Range.
- Cross-provider matches create suggestions only; an administrator confirms every merge.
- v1.0 uses metadata matching only. Audio fingerprinting is deferred.
- Audio has no second copy in v1.0; this accepted risk must be explicit in operations and release records.
- Routine CI uses fixtures. Credentialed provider smoke is manually triggered and protected.

## Evaluation of current v0.2

### Retain

- Durable database claims, ownership fencing, bounded retries, dead-letter states and redrive.
- Supabase JWT/RLS roles and server-only secrets.
- Editorial inbox and explicit publication action.
- Redis/Celery separation and provider safety boundaries.
- Nuxt operator UI and deployed production topology.

### Replace before audio work

- Closed `SetSource` enum and three-value database check.
- One `(source, source_id)` per set.
- Single `ProviderAdapter.fetch(url)` contract.
- Silent cross-provider duplicate terminalization.
- Provider-specific worker routing.
- The global rule “no media storage”; replace it with “no unauthorized acquisition.”

### Verdict

v0.2 is substantially implemented, but release closure is incomplete while CI runner availability, real JWT role verification, credentialed smoke evidence, and observability gates remain open. The deployed metadata platform represents roughly one-third of the approved v1 scope. Ship `v0.2.1` first, then implement provider platform → provider expansion → canonical merge → rights/storage → processing/playback → public archive → hardening.

## Release gates

| Release | Purpose | Exit gate |
|---|---|---|
| `v0.2.1` | Close production evidence | #2, #4, #5, #6 and #12 resolved or explicitly re-scoped |
| `v0.3.0` | Capability provider platform | Existing adapters use one registry; provider-source backfill complete |
| `v0.4.0` | New launch providers | Archive.org, Mixcloud, Audius and RSS pass one contract and protected smoke |
| `v0.5.0` | Canonical merge review | Multiple provider items per set; no automatic merge |
| `v0.6.0` | Rights and MinIO quarantine | Authorized-only private acquisition, review and 30-day expiry |
| `v0.7.0` | Processing and playback | Verified 256 CBR derivative, Range streaming, rights-gated download |
| `v0.8.0` | Editorial/public archive | Images, OCR, settings, public pages and audit complete |
| `v0.9.0` | Production beta | Security, load, capacity, recovery and soak gates pass |
| `v1.0.0` | General availability | Stable APIs, migration rehearsal, exact-commit release evidence |

## File map

### Provider platform

- Create `apps/api/app/services/provider_contracts.py` and `provider_registry.py`.
- Create adapters `archive_org.py`, `mixcloud.py`, `audius.py`, `rss.py`.
- Modify `youtube.py`, `soundcloud.py`, `ftm.py`, `provider.py`, `normalizer.py`.
- Create `apps/api/tests/provider_contract.py` and provider fixtures.
- Create `supabase/migrations/20260730090000_provider_platform.sql`.
- Create `apps/api/app/schemas/provider.py`.

### Canonical merge

- Create `supabase/migrations/20260730100000_merge_candidates.sql`.
- Create `apps/api/app/schemas/merge.py`, `services/merge_suggestions.py`, `routers/merge_candidates.py`.
- Modify all repository implementations and set DTOs.
- Create Nuxt merge-candidate routes and `ProviderSources.vue`.

### Rights and audio

- Create `supabase/migrations/20260730110000_audio_rights.sql`.
- Create `schemas/audio.py`, `services/audio_storage.py`, `audio_acquisition.py`, `audio_processing.py`, `audio_access.py`, `routers/audio.py`, `workers/audio_worker.py`.
- Add MinIO to local and production Compose.
- Create Nuxt audio review routes and `AudioPlayer.vue`.

### Public and operations

- Create public archive routes and DTOs.
- Create `docs/provider-contract.md`, `rights-and-audio.md`, `minio-operations.md`, `release-status.md`, `v1-release-gate.md`.
- Create protected provider-smoke and release-candidate workflows.

---

### Task 1: Close v0.2.1 production evidence

**Files:** Create `docs/release-status.md`; modify `README.md`, `docs/deployment-production.md`, `apps/api/tests/test_infrastructure_contract.py`.

- [ ] Write a failing test requiring CI runner, JWT role matrix, provider smoke and observability gates in the status document.
- [ ] Reconcile #2, #4, #5, #6 and #12 against actual production evidence.
- [ ] Run API/web suites and production restart/redrive checks.
- [ ] Commit: `docs: establish v0.2.1 release gate`.

### Task 2: Restore CI governance

**Files:** Modify `.github/workflows/ci.yml`; create `.github/pull_request_template.md`.

- [ ] Assert CI includes pytest, compileall, web tests, typecheck and build.
- [ ] Fix account/repository Actions allocation rather than masking pre-step runner failures.
- [ ] Require migration, rollback, secret, provider-boundary and public-data reviews.
- [ ] Prove both jobs execute and pass; configure required checks.
- [ ] Commit: `ci: enforce v1 development gates`.

### Task 3: Add provider capability contracts

**Files:** Create `provider_contracts.py`, `provider_registry.py`, `test_provider_registry.py`; modify `provider.py`.

**Produces:** `ProviderCapability`, `ProviderDescriptor`, `ProviderItemPayload`, `DiscoveryRequest`, `DiscoveryPage`, `AuthorizedAudioCandidate`, `ProviderRegistry`.

- [ ] Test that declared capabilities require matching runtime protocols.
- [ ] Implement capabilities: `discover`, `resolve_metadata`, `embed`, `authorized_audio_fetch`, `creator_upload`, `syndicate`, `license_evidence`.
- [ ] Keep compatibility re-exports.
- [ ] Run focused tests; commit `feat: add capability-based provider registry`.

### Task 4: Add provider items and source links

**Files:** Create migration `20260730090000_provider_platform.sql`, `schemas/provider.py`, repository tests; modify set schemas and repositories.

**Produces:** `providers`, `provider_items`, `set_provider_items`, `upsert_provider_item()`, `attach_provider_item()`.

- [ ] Test multiple provider items on one canonical set.
- [ ] Add RLS/grants and backfill every v0.2 set.
- [ ] Dual-write new imports in one transaction.
- [ ] Keep legacy source fields readable as primary-source compatibility through v0.7.
- [ ] Run memory/PostgreSQL contracts; commit `feat: add canonical provider source links`.

### Task 5: Generalize worker routing

**Files:** Modify job schema, Celery config, dispatch, Compose; create `workers/provider_worker.py`, dispatch tests.

- [ ] Test workload routes `provider-api`, `provider-scrape`, `process`, `audio`.
- [ ] Implement generic resolve/discover tasks using durable job UUIDs and registry capability checks.
- [ ] Preserve claims, fencing, retries and redrive.
- [ ] Replace provider-specific worker services without deleting volumes.
- [ ] Commit `refactor: route provider work by capability class`.

### Task 6: Migrate existing adapters

**Files:** Modify YouTube, SoundCloud, FTM, normalizer and tests; create shared provider contract.

- [ ] Make all adapters return `ProviderItemPayload`.
- [ ] Preserve official YouTube API, SoundCloud validation/bounds and FTM robots/delay guarantees.
- [ ] Declare YouTube metadata/embed only; SoundCloud conditional authorized download; FTM metadata/evidence only.
- [ ] Prove metadata-only adapters cannot resolve audio.
- [ ] Commit `refactor: register existing providers`.

### Task 7: Generic provider health and scheduling

**Files:** Modify provider/profile routers, config, schemas, ProviderHealth UI, profile UI and types.

- [ ] Test that a registry-injected provider appears without enumeration edits.
- [ ] Return configured/enabled state, capabilities and workload class.
- [ ] Reject profiles for providers without discovery.
- [ ] Complete #7 with overlap prevention and last/next run display.
- [ ] Commit `feat: drive provider health and schedules from registry`.

### Task 8: Archive.org adapter

**Files:** Create `archive_org.py`, fixtures and `test_archive_org.py`.

- [ ] Test search pagination, normalization, file choice, missing evidence and size bounds.
- [ ] Implement bounded official search/metadata calls.
- [ ] Emit candidates only for downloadable audio with captured evidence fields.
- [ ] Register `discover`, `resolve_metadata`, `embed`, `authorized_audio_fetch`, `license_evidence`.
- [ ] Commit `feat: add Archive.org provider adapter`.

### Task 9: Mixcloud, Audius and RSS adapters

**Files:** Create three adapters and tests; add pinned feedparser dependency.

- [ ] Assert Mixcloud never declares `authorized_audio_fetch`.
- [ ] Implement bounded discovery/metadata/embed normalization.
- [ ] Emit Audius candidates only when download is explicitly permitted.
- [ ] Treat RSS enclosures as candidates only for trusted feeds or explicit evidence.
- [ ] Commit `feat: add Mixcloud Audius and RSS providers`.

### Task 10: Protected provider smoke matrix

**Files:** Create `.github/workflows/provider-smoke.yml`, `scripts/provider-smoke.py`, `docs/provider-contract.md`.

- [ ] Require manual dispatch and protected environment secrets.
- [ ] Create/poll one job per configured provider and upload sanitized JSON evidence.
- [ ] Assert no auto-publication and zero audio assets for metadata-only providers.
- [ ] Update #5 wording to “no unauthorized media acquisition.”
- [ ] Commit `test: add protected provider smoke matrix`.

### Task 11: Metadata merge suggestions

**Files:** Create migration `20260730100000_merge_candidates.sql`, merge schemas/service/tests; modify repositories and import pipeline.

**Produces:** states `pending`, `approved`, `rejected`, `superseded`; `score_merge()` with stored component reasons.

- [ ] Test normalized artist/title, event, date/year, duration tolerance, aliases and unrelated sets.
- [ ] Keep exact provider identity idempotent.
- [ ] Create pending cross-provider candidates instead of silent duplicate completion.
- [ ] Store deterministic component scores; no external service or audio fingerprint.
- [ ] Commit `feat: create explainable merge suggestions`.

### Task 12: Manual merge review and undo

**Files:** Create merge router, Nuxt review pages, source component and API/UI tests.

- [ ] Test admin-only authorization, stale `409`, transactional failure and retained sources.
- [ ] Move source links and evidence without hard-deleting the losing set.
- [ ] Store before/after audit identifiers for restore.
- [ ] Build side-by-side score/reason UI with typed confirmation.
- [ ] Commit `feat: add manual source merge review`.

### Task 13: Rights and audio schema

**Files:** Create migration `20260730110000_audio_rights.sql`, `schemas/audio.py`, repository methods/tests.

**Produces:** `rights_evidence`, `audio_permissions`, `audio_assets`, `audio_versions`, `audio_reviews`; states `quarantine`, `approved`, `rejected`, `expired`, `processing`, `ready`, `failed`.

- [ ] Test permission and state invariants.
- [ ] Store immutable evidence payload hashes and actor/timestamps.
- [ ] Expose public rows only for ready assets on published sets with active stream permission.
- [ ] Restrict rights/download approval to admins.
- [ ] Commit `feat: add rights and audio asset domain`.

### Task 14: Private MinIO storage

**Files:** Create `audio_storage.py`, storage tests and operations docs; modify config, requirements, Compose and env examples.

**Produces:** `put_stream`, `stat`, `open_range`, `copy`, `delete`; buckets `audio-quarantine`, `audio-originals`, `audio-derivatives`.

- [ ] Test multipart writes, checksums, ranges, traversal rejection, promotion and deletion.
- [ ] Generate opaque server-side keys only.
- [ ] Keep MinIO internal; never expose S3 or console through Caddy.
- [ ] Use persistent volumes and document the accepted lack of a second audio copy.
- [ ] Commit `feat: add private MinIO audio storage`.

### Task 15: Authorized acquisition and creator uploads

**Files:** Create `audio_acquisition.py`, audio router/worker and tests; modify worker config and app registration.

- [ ] Test YouTube/Mixcloud rejection before network access.
- [ ] Require matching capability and evidence for provider acquisition.
- [ ] Revalidate redirects; block private/link-local targets; cap bytes/time/type.
- [ ] Stream directly to quarantine with SHA-256.
- [ ] Add resumable creator upload with final rights attestation.
- [ ] Commit `feat: quarantine authorized audio inputs`.

### Task 16: Rights review and 30-day expiry

**Files:** Modify audio router/worker/recovery; create Nuxt audio review routes and tests.

- [ ] Use frozen-clock tests for approval, rejection, expiry and idempotent cleanup.
- [ ] Require admin evidence review and notes.
- [ ] Permit download only when evidence supports it.
- [ ] Claim expiry rows in the database; delete bytes and retain an audit tombstone.
- [ ] Commit `feat: add rights review and quarantine expiry`.

### Task 17: Probe, preserve and transcode

**Files:** Create `audio_processing.py`, audio fixtures/tests; modify worker and requirements.

- [ ] Test valuable-original retention, good-MP3 reuse, derivative generation and corrupt input.
- [ ] Run ffprobe/FFmpeg with arrays, no shell, clean environment, timeout and bounded stderr.
- [ ] Encode `libmp3lame -b:a 256k` only when required.
- [ ] Verify codec, duration, channels, sample rate, bitrate and tags after upload.
- [ ] Commit `feat: create verified MP3 stream derivatives`.

### Task 18: Rights-aware HTTP Range access

**Files:** Create `audio_access.py`, Range tests; modify audio router and Caddy behavior.

- [ ] Test full/prefix/suffix/open-ended/invalid ranges, HEAD, ETag and If-Range.
- [ ] Resolve publication, ready version and permissions before touching storage.
- [ ] Separate public stream from public download authorization.
- [ ] Add rate/concurrency controls and byte/error metrics.
- [ ] Commit `feat: serve policy-checked ranged audio`.

### Task 19: Public player and source UI

**Files:** Create `AudioPlayer.vue`, `ProviderSources.vue`, unit/E2E tests; modify set/inbox pages and types.

- [ ] Test keyboard controls, reduced motion, permission-gated download and source fallback.
- [ ] Use native `<audio>` for standard Range behavior.
- [ ] Do not add waveform generation in v1.
- [ ] E2E-test seek without full-file prefetch.
- [ ] Commit `feat: add canonical set audio player`.

### Task 20: Images, OCR and persisted settings

**Files:** Implement existing issues #8, #9 and #10 against canonical sets.

- [ ] Attach images and provenance to canonical sets/provider items.
- [ ] Keep pHash and OCR results as human-reviewed evidence only.
- [ ] Persist versioned heuristic configuration through admin API/UI.
- [ ] Execute as three independent TDD PRs and commits.

### Task 21: Public archive, SEO and audit

**Files:** Implement #11; create public router, archive/artist/event pages and leak tests.

- [ ] Test public DTOs exclude raw payloads, candidates, evidence, notes, object keys and diagnostics.
- [ ] Return only published canonical records.
- [ ] Add canonical/Open Graph/Schema.org metadata only when data exists.
- [ ] Revoke access before authorized deletion cleanup.
- [ ] Commit `feat: publish canonical set archive`.

### Task 22: Media observability and capacity

**Files:** Create `services/metrics.py`, `docs/observability.md`, metrics tests; modify health/audio worker/MinIO docs.

- [ ] Cover database, Redis, beat, provider queues, audio queue, MinIO, quarantine age, processing latency and bytes served.
- [ ] Keep metric labels low-cardinality and secret-free.
- [ ] Alert on stale claims, dead letters, MinIO failure, disk thresholds, expiry and stream errors.
- [ ] Extend and close/supersede #6 with media evidence.
- [ ] Commit `feat: observe provider and audio pipelines`.

### Task 23: Security, privacy and recovery

**Files:** Create threat model, recovery drill docs/script, SSRF and public-leak tests; modify deployment docs.

- [ ] Test DNS rebinding, private/link-local addresses, redirects, size, MIME, traversal and range amplification.
- [ ] Enforce scheme/IP/redirect/time/size boundaries and secret redaction.
- [ ] Restore PostgreSQL into a fresh database and reconstruct Redis from job truth.
- [ ] Recreate MinIO configuration and demonstrate documented unrecoverable object loss.
- [ ] Commit `security: harden media trust boundaries`.

### Task 24: v0.9 production beta gates

**Files:** Create release-candidate workflow, Range load and beta-soak scripts, `docs/v1-release-gate.md`.

- [ ] Require protected manual dispatch for an exact commit.
- [ ] Mix full, partial, invalid and concurrent Range requests.
- [ ] Repeatedly exercise discovery, quarantine, approval, transcode, stream, expiry and provider outage.
- [ ] Fail on unauthorized assets, auto-merge, auto-publication, leaks, stuck jobs or failed restore.
- [ ] Commit `test: add v1 release candidate gates`.

### Task 25: Stabilize APIs and release v1.0.0

**Files:** Create `20260730120000_v1_cleanup.sql`, `CHANGELOG.md`, v1 contract/E2E tests; modify app schemas/docs.

- [ ] Freeze `/api/v1` and `/public/v1` golden JSON contracts.
- [ ] Rehearse all migrations from clean and v0.2 snapshots; verify backfill counts before cleanup.
- [ ] Run full pytest/compileall, web tests/typecheck/build/Playwright, Compose, protected provider smoke and release-candidate gates.
- [ ] Confirm no open P0/critical issue and publish accepted-risk evidence.
- [ ] Commit `release: prepare SETCRAWLER v1.0.0`; create signed `v1.0.0` tag.

## Existing issue mapping

- `v0.2.1`: #2, #4, #5, #6, #12.
- `v0.3`: #7.
- `v0.8`: #8, #9, #10, #11.

## Release epics

- #18 `v0.2.1`
- #19 `v0.3`
- #20 `v0.4`
- #21 `v0.5`
- #22 `v0.6`
- #23 `v0.7`
- #24 `v0.8`
- #25 `v0.9`
- #26 `v1.0`
- #27 master roadmap

## Deferred beyond v1.0

- Chromaprint/AcoustID and partial-segment matching.
- Automatic merge approval.
- Audio acquisition from YouTube or Mixcloud streams.
- Cross-region or second-copy audio backup.
- Waveforms, native apps, billing and revenue distribution.

## Self-review

- Every approved provider, rights, storage, retention, access, merge and encoding decision maps to a task.
- The plan contains no unspecified implementation placeholders.
- Capability, source-link, merge-state, audio-state and permission names are consistent across tasks.
- The lack of a second audio copy is treated as an explicit accepted risk, not an accidental omission.
