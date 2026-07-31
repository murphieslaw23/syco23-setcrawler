# SYCO23 SETCRAWLER v0.3 capability-based provider platform

**Status:** Approved design

**Date:** 2026-07-31

**Owning issue:** #19

**Base release:** v0.2.1 repository baseline merged by PR #29

## 1. Purpose

v0.3 replaces the closed provider enum, provider-specific routing, and single-source assumptions with an extensible provider platform. The release must preserve the current API and editorial behavior while creating the stable contracts needed for Archive.org, Mixcloud, Audius, RSS, canonical multi-source sets, and later rights-aware audio ingestion.

The migration is additive. Existing `sets.source` and `sets.source_id` fields remain readable and writable during v0.3. New provider tables and links become the authoritative extension point, but removal of legacy source fields is deferred until downstream releases have migrated.

## 2. Goals

v0.3 must:

1. Define explicit provider capabilities for discovery, metadata, embeds, authorized audio, creator uploads, syndication, and license evidence.
2. Register providers through validated descriptors rather than a closed source enum or provider-specific conditionals.
3. Store provider identity and provider items independently from canonical set records.
4. Backfill every existing set with exactly one source link without changing its public identity or review state.
5. Dual-write legacy source fields and new provider links for all existing import paths.
6. Route work by workload class rather than provider name.
7. Migrate YouTube, SoundCloud, and freeteknomusic.org adapters into the registry.
8. Make provider health and search-profile behavior descriptor-driven.
9. Preserve the no-auto-publish invariant and all current acquisition restrictions.
10. Keep the full API, migration, durable-job, provider fixture, web test, typecheck, and production-build suites green.

## 3. Non-goals

v0.3 does not:

- add Archive.org, Mixcloud, Audius, or RSS adapters;
- enable media acquisition, stream ripping, audio downloads, MinIO writes, derivatives, or playback;
- automatically merge duplicate sets;
- remove `sets.source` or `sets.source_id`;
- introduce Chromaprint or other audio fingerprinting;
- change manual rights approval or publication policy;
- change the public archive information architecture;
- complete production JWT, provider-credential, recovery, or observability evidence still tracked under the v0.2.1 operational gates.

## 4. Chosen migration strategy

### 4.1 Additive schema and dual-write

The release uses an additive migration with a compatibility interval.

- New source relationships are written to `providers`, `provider_items`, and `set_provider_items`.
- Legacy `sets.source` and `sets.source_id` remain populated.
- Reads may continue to use legacy fields where API compatibility requires it.
- New provider-platform code reads through repository interfaces that can resolve the linked provider item first and fall back to legacy fields during migration.
- A database backfill creates exactly one provider-item link for every existing set with a valid legacy source pair.

This strategy is preferred over an immediate cutover because it provides deterministic rollback, limits the blast radius, and allows compatibility checks to compare old and new representations.

### 4.2 Authority during v0.3

During v0.3:

- the registry is authoritative for provider behavior and capabilities;
- `provider_items` is authoritative for provider-specific identity and normalized source metadata;
- `set_provider_items` is authoritative for relationships between sets and external sources;
- legacy source fields remain a required compatibility projection;
- publication state remains authoritative on the canonical set record.

Conflicting legacy and linked source data is an integrity error. It must be surfaced by tests, health diagnostics, or operator-visible validation rather than silently reconciled.

## 5. Provider capability model

### 5.1 Capability identifiers

The initial capability vocabulary is:

- `discovery`: enumerate or search provider content through a permitted interface;
- `metadata`: fetch normalized metadata for a known provider item or URL;
- `embed`: expose an externally hosted, provider-approved embed or canonical playback URL;
- `authorized_audio`: acquire audio only when a rights-aware adapter can prove the operation is permitted;
- `creator_upload`: accept media supplied directly by an authenticated rights holder;
- `syndication`: consume feed-style publication such as RSS or Atom;
- `license_evidence`: return source-derived evidence relevant to rights review.

Capabilities are declarative. A provider advertising a capability must supply the corresponding adapter interface and tests.

### 5.2 Capability safety rules

Registry validation must reject:

- duplicate provider keys;
- unknown capability names;
- a capability without its required adapter method;
- an adapter method exposed without the corresponding declared capability;
- `authorized_audio` without an explicit rights-decision function and a deny-by-default path;
- `creator_upload` without authenticated ownership and license-evidence contracts;
- unsupported workload classes;
- an enabled provider without a usable adapter factory;
- configuration schemas that expose secrets in public descriptor output.

No current provider declares `authorized_audio` or `creator_upload` in v0.3.

## 6. Provider descriptor and registry

### 6.1 Descriptor contract

Each provider descriptor contains:

- stable lowercase `key` suitable for database and job payloads;
- human-readable `display_name`;
- immutable provider identity metadata;
- declared capability set;
- workload class for each executable capability;
- adapter factory or adapter implementation reference;
- health-check definition;
- configuration schema containing secret names but never secret values;
- source URL matching rules;
- optional search-profile schema;
- enabled-by-default flag;
- operational limits such as request timeout, output limit, and concurrency class.

Provider descriptors are application code. Database rows mirror operational identity and selected public metadata but do not dynamically load arbitrary executable classes.

### 6.2 Registry lifecycle

The registry is constructed once during API and worker startup.

Startup sequence:

1. import built-in provider descriptors;
2. validate each descriptor independently;
3. validate cross-provider uniqueness and URL matcher ambiguity;
4. instantiate only enabled adapters;
5. expose an immutable registry to services, routes, schedulers, and workers;
6. fail startup with a concise validation error if any invariant is broken.

Tests may construct isolated registries with fixture providers. Adding a fixture provider must require only descriptor registration and tests, not edits to an enum or central `if/elif` dispatch block.

### 6.3 Initial descriptors

Initial provider capabilities are intentionally conservative:

- **YouTube:** `discovery`, `metadata`, `embed`; workload class `provider-api`.
- **SoundCloud:** `metadata`, `embed`; workload class `provider-scrape` for the isolated metadata extractor. No stream ripping or download capability.
- **freeteknomusic.org:** `discovery`, `metadata`, `license_evidence`; workload class `provider-scrape`.

Capability declarations must reflect implemented and legally permitted behavior, not theoretical provider features.

## 7. Data model

### 7.1 `providers`

Purpose: stable identity and operational metadata for each registered provider.

Required fields:

- `id` UUID primary key;
- `key` text, unique, immutable after creation;
- `display_name` text;
- `capabilities` JSONB or text array with a database-level shape check;
- `enabled` boolean;
- `workload_policy` JSONB containing capability-to-workload mappings;
- `descriptor_version` integer;
- `created_at` and `updated_at` timestamps.

The application synchronizes built-in descriptors to provider rows idempotently. Secret values are never stored in this table.

### 7.2 `provider_items`

Purpose: represent one externally addressable object at one provider independently from a canonical SETCRAWLER set.

Required fields:

- `id` UUID primary key;
- `provider_id` foreign key to `providers`;
- `external_id` text;
- `canonical_url` text;
- `item_type` text, initially `set_candidate` unless a provider supplies a more precise supported type;
- `title` text nullable;
- `published_at` timestamp nullable;
- `duration_seconds` integer nullable with non-negative check;
- `embed_url` text nullable;
- `raw_metadata` JSONB containing sanitized provider metadata;
- `metadata_fetched_at` timestamp nullable;
- `created_at` and `updated_at` timestamps.

Uniqueness is enforced on `(provider_id, external_id)`. Canonical URL indexes support URL-based lookup, but URLs are not assumed immutable or globally unique.

### 7.3 `set_provider_items`

Purpose: relate a canonical set to one or more provider items.

Required fields:

- `set_id` foreign key to `sets`;
- `provider_item_id` foreign key to `provider_items`;
- `relationship` text, initially `source`;
- `is_primary` boolean;
- `created_at` timestamp;
- composite primary key or unique constraint on `(set_id, provider_item_id, relationship)`.

v0.3 enforces at most one primary source link per set. It does not prevent future secondary links needed by v0.5. Existing sets receive exactly one primary `source` link during backfill.

### 7.4 Row-level security

RLS must preserve current visibility rules:

- public users may see provider information linked to published sets only through approved API projections;
- editor roles may read source links required for review;
- administrators may maintain provider records and source relationships;
- direct public writes are denied;
- service-role operations remain server-side only.

Raw provider metadata must not be exposed wholesale through public endpoints.

## 8. Migration and backfill

### 8.1 Migration ordering

The migration sequence is:

1. create tables, constraints, indexes, and RLS policies;
2. insert or synchronize built-in provider rows for YouTube, SoundCloud, and FTM;
3. backfill provider items from every valid `(sets.source, sets.source_id)` pair;
4. create exactly one primary `set_provider_items` link per existing set;
5. run integrity assertions that abort the migration on missing or duplicate links;
6. leave legacy fields unchanged.

### 8.2 Backfill mapping

Legacy source values are mapped through an explicit migration mapping table or deterministic SQL expression. Unknown legacy values abort the migration instead of being coerced into a generic provider.

For each existing set:

- resolve the provider row from `sets.source`;
- upsert a provider item by `(provider_id, sets.source_id)`;
- copy the canonical URL and safe normalized metadata already present on the set where available;
- insert one primary source relationship;
- verify the relationship resolves back to the same legacy provider key and external ID.

### 8.3 Migration contracts

Automated tests must prove:

- the migration applies to an empty database;
- the migration applies to a representative v0.2 dataset;
- every existing set has exactly one primary source link after backfill;
- no provider item is duplicated for the same provider and external ID;
- unknown legacy sources fail loudly;
- rerunning idempotent synchronization does not create duplicates;
- legacy fields are unchanged;
- all prior migrations still apply in deterministic filename order.

## 9. Repository and service behavior

### 9.1 Normalized provider item

Adapters return a provider-neutral value object containing:

- provider key;
- external ID;
- canonical URL;
- normalized title, publication time, duration, creator/channel metadata, and artwork candidates;
- optional embed URL;
- sanitized raw metadata;
- provenance and license-evidence fields where declared;
- deterministic deduplication inputs.

The object contains metadata only in v0.3. It cannot contain downloaded media bytes or local media paths.

### 9.2 Dual-write transaction

Creating or updating a set from a provider import must perform, in one database transaction where supported:

1. resolve the provider descriptor and provider row;
2. upsert the provider item;
3. create or update the canonical set;
4. write the primary source relationship;
5. write the legacy `source` and `source_id` compatibility projection;
6. assert that both representations agree before commit.

If any step fails, the job remains retryable under the existing durable-job rules. Partial source relationships must not be committed.

### 9.3 Read compatibility

Existing API response fields remain unchanged in v0.3. Internally, repositories may derive the response source fields from the primary provider link and compare them to legacy values. Any mismatch is logged as an integrity event and returned as a controlled server error for mutation paths rather than silently overwriting data.

## 10. Workload-based routing

### 10.1 Workload classes

The queue vocabulary becomes:

- `provider-api`: rate-limited provider APIs and structured remote metadata calls;
- `provider-scrape`: isolated HTML or command-based metadata extraction with strict time and output limits;
- `process`: normalization, scoring, candidate extraction, deduplication, and image metadata processing;
- `audio`: reserved for future authorized media work; no v0.3 task may enqueue it.

Provider descriptors map capabilities to workload classes. Jobs contain provider key, capability, operation, and normalized arguments; the worker resolves the adapter from the registry.

### 10.2 Compatibility and rollout

Provider-specific queue names may remain as temporary aliases during deployment. New scheduler and enqueue code targets workload queues. Workers may listen to both old and new names for one compatibility window, after which provider-specific aliases can be removed in a separate cleanup change.

Existing durable claim, retry, dead-letter, stale-job, redrive, and idempotency semantics remain unchanged.

### 10.3 Generic scheduling

Search profiles reference provider key and a descriptor-defined operation. The scheduler:

1. loads enabled profiles due for execution;
2. resolves the provider and verifies `discovery` capability;
3. validates profile parameters against the descriptor schema;
4. creates a durable job with the appropriate workload class;
5. advances schedule state only after durable job creation succeeds.

Provider-specific cron branches are prohibited.

## 11. Health and operator surfaces

Provider health output is generated from descriptors and runtime state. It includes:

- provider key and display name;
- enabled state;
- declared capabilities;
- configuration completeness without revealing secret values;
- last successful and failed operation timestamps where available;
- workload class;
- degraded or unavailable status with sanitized reason.

Search-profile forms and validation use descriptor schemas. The existing UI remains compatible for current providers; generic rendering may be introduced incrementally, but provider-specific UI logic must not expand.

## 12. Failure handling

The platform distinguishes:

- `provider_not_registered`;
- `provider_disabled`;
- `capability_not_supported`;
- `provider_configuration_missing`;
- `provider_rate_limited`;
- `provider_unavailable`;
- `provider_payload_invalid`;
- `source_integrity_mismatch`;
- `rights_not_permitted`.

Errors are normalized before durable-job classification. Retryability depends on category, not provider-specific string matching. Validation, rights denial, and integrity mismatch are non-retryable until configuration or data changes. Rate limits and transient provider failures use bounded retry policies with jitter.

Logs and job results must redact credentials, bearer tokens, cookies, database URLs, private payloads, and command output beyond configured limits.

## 13. Security and rights invariants

- Registry descriptors contain secret identifiers only, never values.
- Provider URLs and external IDs are validated and length-bounded.
- Scrape-style adapters execute in the existing isolated environment with timeout and output limits.
- Raw metadata is sanitized before persistence.
- No provider in v0.3 may expose audio acquisition methods.
- Any future `authorized_audio` implementation must be deny-by-default and require explicit rights evidence.
- Publication remains a separate manual editorial action.
- Duplicate detection remains advisory; no automatic merge is introduced.

## 14. Testing strategy

### 14.1 Unit and contract tests

Tests cover:

- descriptor validation and invalid capability combinations;
- duplicate keys and ambiguous URL matchers;
- fixture-provider registration without core enum edits;
- adapter capability conformance;
- workload routing;
- normalized error mapping;
- dual-write consistency;
- descriptor-driven health and search-profile validation;
- prohibition of audio capability and audio queue usage in v0.3.

### 14.2 Integration tests

PostgreSQL integration tests cover:

- schema migration and backfill;
- provider/item/link uniqueness;
- transaction rollback on partial dual-write failure;
- repository reads through provider links with legacy projection;
- durable job retries and idempotency under workload routing;
- RLS and public projection boundaries.

### 14.3 Existing regression suites

The following must remain green on every implementation PR:

- all existing provider fixture tests;
- durable job, retry, dead-letter, stale claim, and redrive tests;
- complete API pytest suite against PostgreSQL and Redis;
- Python compileall;
- complete web Vitest suite;
- Nuxt typecheck;
- Nuxt production build.

## 15. Rollout and rollback

### 15.1 Rollout

Implementation lands in five bounded slices:

1. provider capability contracts and registry;
2. additive schema and deterministic backfill;
3. repository dual-write and API compatibility;
4. existing adapter migration and workload routing;
5. descriptor-driven health, search profiles, and generic scheduling.

Each slice is independently testable and must not enable unfinished runtime behavior.

### 15.2 Deployment order

For schema-bearing slices:

1. deploy additive migration;
2. verify backfill counts and integrity queries;
3. deploy dual-write-capable API and workers;
4. verify jobs create matching legacy and linked sources;
5. switch scheduler enqueue targets to workload queues;
6. retain temporary queue aliases through the compatibility window.

### 15.3 Rollback

Application rollback returns to the previous image while leaving additive tables intact. Legacy fields continue to support v0.2 code. Rollback must not drop provider tables, delete provider links, truncate jobs, remove PostgreSQL history, or remove Redis AOF data.

A schema rollback is not part of the normal recovery path. Corrective forward migrations are preferred after any production write has used the new tables.

## 16. Pull-request boundaries

### PR 1 — Registry foundation

- capability vocabulary and typed contracts;
- descriptor and registry validation;
- YouTube, SoundCloud, and FTM descriptors wrapping existing behavior;
- fixture-provider extension test;
- no schema or routing changes.

### PR 2 — Provider source schema

- `providers`, `provider_items`, `set_provider_items` migration;
- built-in provider synchronization;
- complete v0.2 backfill;
- integrity and RLS tests.

### PR 3 — Dual-write compatibility

- provider-item repository interfaces;
- transactional dual-write;
- legacy projection and mismatch detection;
- API compatibility tests.

### PR 4 — Adapter and queue migration

- adapter resolution through registry;
- workload-class jobs and workers;
- temporary queue aliases;
- normalized error categories;
- no `audio` tasks.

### PR 5 — Descriptor-driven operations

- generic scheduling;
- descriptor-defined search-profile schemas;
- provider health output;
- operator-surface compatibility;
- completion evidence for issue #19 and applicable generic scheduling work from #7.

## 17. Exit criteria

v0.3 is complete only when:

1. a fixture provider can be added through registration and tests without editing a closed source enum or central provider dispatch branch;
2. all existing sets have exactly one valid backfilled primary source link;
3. all current import paths dual-write consistent legacy and provider-link data;
4. YouTube, SoundCloud, and FTM operate through the registry;
5. scheduler and workers route by workload class;
6. provider health and search-profile validation are descriptor-driven;
7. existing API responses remain compatible;
8. no media acquisition or audio storage behavior is enabled;
9. all full CI gates pass on the exact release commit;
10. rollback instructions are verified against the additive schema strategy.
