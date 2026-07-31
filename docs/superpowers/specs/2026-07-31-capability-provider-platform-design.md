# SYCO23 SETCRAWLER v0.3 capability-based provider platform

**Status:** Approved design  
**Date:** 2026-07-31  
**Owning issue:** #19  
**Base:** v0.2.1 baseline merged by PR #29

## 1. Purpose

v0.3 replaces the closed provider enum, provider-specific dispatch, and single-source assumptions with an extensible provider platform. It preserves the current API and editorial behavior while establishing the contracts needed for Archive.org, Mixcloud, Audius, RSS, canonical multi-source sets, and later rights-aware audio ingestion.

The migration is additive. Existing `sets.source` and `sets.source_id` fields remain required compatibility projections throughout v0.3. New provider tables and links become the extension point, but legacy fields are not removed in this release.

## 2. Goals

v0.3 must:

1. Define capabilities for discovery, metadata, embeds, authorized audio, creator uploads, syndication, and license evidence.
2. Register providers through validated descriptors rather than a closed enum or central provider conditionals.
3. Separate provider identity and provider items from canonical set records.
4. Backfill every existing set with exactly one primary source link without changing its identity, score, or review state.
5. Dual-write legacy source fields and provider source links for every current import path.
6. Route jobs by workload class rather than provider name.
7. Migrate YouTube, SoundCloud, and freeteknomusic.org to the registry.
8. Make provider health and search-profile validation descriptor-driven.
9. Preserve manual publication, rights approval, and all current acquisition restrictions.
10. Keep the complete API and web CI gates green.

## 3. Non-goals

v0.3 does not:

- add Archive.org, Mixcloud, Audius, or RSS adapters;
- enable downloads, stream ripping, MinIO writes, derivatives, playback, or any audio acquisition;
- automatically merge duplicates;
- remove `sets.source` or `sets.source_id`;
- add audio fingerprinting;
- change publication policy;
- complete the remaining production-evidence gates tracked separately under v0.2.1.

## 4. Chosen strategy

### 4.1 Additive schema and dual-write

New source relationships are written to `providers`, `provider_items`, and `set_provider_items`. Legacy fields remain populated. Repository code resolves the primary provider link first and verifies that the legacy projection agrees with it.

A mismatch is an integrity error. Mutation paths fail rather than silently choosing one representation. Read-only diagnostics may report the mismatch in sanitized health output.

This strategy is preferred because it supports deterministic rollback to v0.2 application code without dropping new tables or reconstructing legacy fields.

### 4.2 Authority during v0.3

- Provider descriptors in application code are authoritative for behavior, capabilities, adapter construction, configuration shape, URL matching, and workload routing.
- The `providers.enabled` database flag is an operational kill switch.
- A provider is effectively enabled only when it is registered in code, enabled in the database, and has complete required configuration.
- `provider_items` is authoritative for provider-specific identity and normalized source metadata.
- `set_provider_items` is authoritative for set-to-source relationships.
- `sets.source` and `sets.source_id` remain mandatory compatibility projections.
- Publication state remains authoritative on the canonical set record.

Database rows never identify arbitrary executable classes and never store secret values.

## 5. Capability model

The capability vocabulary is fixed for v0.3:

- `discovery`: enumerate or search permitted provider content;
- `metadata`: fetch normalized metadata for a known item or URL;
- `embed`: expose a provider-approved external embed or playback URL;
- `authorized_audio`: acquire audio only after an explicit rights decision;
- `creator_upload`: accept media supplied by an authenticated rights holder;
- `syndication`: consume feed-style publication such as RSS or Atom;
- `license_evidence`: return source-derived evidence for rights review.

Capabilities are declarative contracts. Advertising a capability requires the matching adapter interface and tests.

Registry validation rejects:

- duplicate provider keys;
- unknown capabilities;
- missing adapter methods for declared capabilities;
- capability methods not declared by the descriptor;
- ambiguous URL matchers;
- unsupported workload classes;
- enabled providers without adapter factories;
- public descriptor output containing secret values;
- `authorized_audio` without a deny-by-default rights-decision method;
- `creator_upload` without authenticated ownership and license-evidence contracts.

No v0.3 provider declares `authorized_audio` or `creator_upload`.

## 6. Provider descriptor and registry

Each descriptor contains:

- stable lowercase `key`;
- `display_name`;
- capability set;
- capability-to-workload mapping;
- adapter factory;
- health-check definition;
- configuration schema containing variable names and validation rules, never values;
- URL matchers;
- optional search-profile schema;
- enabled-by-default value used only when the provider row is first created;
- timeout, output-size, and concurrency limits.

The registry is constructed during API and worker startup:

1. import built-in descriptors;
2. validate each descriptor;
3. validate global key and URL-matcher uniqueness;
4. synchronize provider rows idempotently without overwriting an existing operational `enabled` choice;
5. instantiate effectively enabled adapters;
6. expose an immutable registry;
7. fail startup with a concise validation error if any invariant is broken.

Tests may construct isolated registries. Adding a fixture provider must require descriptor registration and tests only, with no source-enum or central dispatch edits.

Initial descriptors are conservative:

- **YouTube** (`youtube`): `discovery`, `metadata`, `embed`; workload `provider-api`.
- **SoundCloud** (`soundcloud`): `metadata`, `embed`; workload `provider-scrape`; no download behavior.
- **freeteknomusic.org** (`ftm`): `discovery`, `metadata`, `license_evidence`; workload `provider-scrape`.

## 7. Data model

### 7.1 `providers`

Required columns:

- `id uuid primary key`;
- `key text not null unique`;
- `display_name text not null`;
- `capabilities text[] not null` with a check that every value belongs to the fixed vocabulary;
- `enabled boolean not null`;
- `workload_policy jsonb not null` containing capability-to-workload mappings;
- `descriptor_version integer not null`;
- `created_at` and `updated_at` timestamps.

Provider keys are immutable after insertion. Secrets are prohibited.

### 7.2 `provider_items`

Required columns:

- `id uuid primary key`;
- `provider_id uuid not null references providers(id)`;
- `external_id text not null`;
- `canonical_url text not null`;
- `item_type text not null default 'set_candidate'`;
- nullable normalized `title`, `published_at`, `duration_seconds`, and `embed_url`;
- `raw_metadata jsonb not null default '{}'`, sanitized before persistence;
- nullable `metadata_fetched_at`;
- `created_at` and `updated_at` timestamps.

Constraints:

- unique `(provider_id, external_id)`;
- non-negative duration when present;
- bounded non-empty external IDs and URLs;
- canonical URL index for lookup, without treating URLs as immutable identity.

### 7.3 `set_provider_items`

Required columns:

- `set_id uuid not null references sets(id)`;
- `provider_item_id uuid not null references provider_items(id)`;
- `relationship text not null default 'source'`;
- `is_primary boolean not null default false`;
- `created_at` timestamp.

Constraints:

- unique `(set_id, provider_item_id, relationship)`;
- partial unique index on `set_id` where `relationship = 'source' and is_primary`;
- v0.3 backfill creates exactly one primary source per existing set;
- secondary source links remain structurally possible for v0.5 but are not created automatically in v0.3.

### 7.4 RLS and exposure

- Public users receive provider data only through approved projections linked to published sets.
- Editors can read source links needed for review.
- Administrators can maintain provider rows and relationships.
- Direct public writes are denied.
- Service-role operations remain server-side.
- Raw provider metadata is never exposed wholesale through public endpoints.

## 8. Migration and backfill

The migration order is fixed:

1. create tables, constraints, indexes, and RLS policies;
2. insert `youtube`, `soundcloud`, and `ftm` provider rows;
3. create a migration-local `legacy_provider_keys` mapping using explicit accepted legacy values and aliases already present in the v0.2 enum;
4. assert that every non-null legacy source maps to exactly one provider key;
5. upsert provider items from `(sets.source, sets.source_id)`;
6. create one primary source link per set;
7. assert that every existing set has exactly one primary source link and that it resolves back to the same legacy source pair;
8. leave legacy fields unchanged.

Unknown legacy sources abort the migration. They are never coerced to a generic provider.

Migration tests must prove:

- clean-database application;
- application over representative v0.2 data;
- one primary source per existing set;
- provider-item uniqueness;
- failure on unknown legacy sources;
- idempotent provider synchronization;
- unchanged legacy fields;
- deterministic execution with all earlier migrations.

## 9. Normalized provider item and dual-write

Adapters return a metadata-only value object containing:

- provider key;
- external ID;
- canonical URL;
- normalized title, publication time, duration, creator/channel metadata, and artwork candidates;
- optional embed URL;
- sanitized raw metadata;
- provenance and license evidence when declared;
- deterministic deduplication inputs.

It cannot contain downloaded media bytes or local media paths.

Creating or updating a set from an import performs one transaction:

1. resolve descriptor and provider row;
2. upsert provider item;
3. create or update canonical set;
4. write primary source relationship;
5. write legacy `source` and `source_id`;
6. assert both representations agree;
7. commit.

Any failure rolls back the transaction. Existing durable-job retry, dead-letter, stale-claim, redrive, and idempotency semantics remain in force.

Existing API response fields do not change in v0.3.

## 10. Workload routing

The queue vocabulary becomes:

- `provider-api`: rate-limited structured provider API calls;
- `provider-scrape`: isolated HTML or command-based metadata extraction with strict limits;
- `process`: normalization, scoring, candidate extraction, deduplication, and image metadata processing;
- `audio`: reserved for later authorized media work; no v0.3 code may enqueue it.

Jobs contain provider key, capability, operation, and normalized arguments. Workers resolve adapters through the registry.

Provider-specific queue names may remain temporary aliases for one deployment compatibility window. New enqueue and scheduler code targets workload queues. Workers can listen to old and new names during that window; removal of aliases is a separate cleanup after production verification.

## 11. Generic scheduling

Search profiles reference provider key plus a descriptor-defined operation. The scheduler:

1. loads due enabled profiles;
2. resolves the provider and verifies `discovery`;
3. validates parameters against the descriptor schema;
4. creates a durable job on the mapped workload queue;
5. advances schedule state only after durable job creation succeeds.

Provider-specific cron branches are prohibited.

## 12. Health and operator behavior

Provider health is generated from descriptors and runtime state. It reports:

- key and display name;
- effective and database enabled states;
- capabilities;
- configuration completeness without values;
- workload mappings;
- sanitized degraded or unavailable reason;
- last successful and failed operation timestamps when available.

Search-profile validation and forms consume descriptor schemas. Existing current-provider UI remains compatible. Provider-specific UI branches must not expand.

## 13. Error handling

Normalized error categories are:

- `provider_not_registered`;
- `provider_disabled`;
- `capability_not_supported`;
- `provider_configuration_missing`;
- `provider_rate_limited`;
- `provider_unavailable`;
- `provider_payload_invalid`;
- `source_integrity_mismatch`;
- `rights_not_permitted`.

Retryability is category-based, not provider-string-based. Validation, rights denial, and integrity mismatch are non-retryable until configuration or data changes. Rate limits and transient provider failures use bounded retries with jitter.

Logs and job results redact credentials, tokens, cookies, database URLs, private payloads, and command output beyond configured limits.

## 14. Security and rights invariants

- Descriptors identify secret variables but never contain values.
- URLs and external IDs are validated and bounded.
- Scrape adapters remain isolated with time and output limits.
- Raw metadata is sanitized before persistence.
- No v0.3 provider exposes media acquisition.
- Future authorized audio remains deny-by-default and requires explicit evidence.
- Publication remains a separate manual editorial action.
- Duplicate detection remains advisory; automatic merge is prohibited.

## 15. Testing strategy

Unit and contract tests cover:

- descriptor validation and capability conformance;
- duplicate keys and ambiguous matchers;
- fixture-provider extension without core dispatch edits;
- workload routing;
- normalized errors;
- dual-write consistency;
- descriptor-driven health and profile validation;
- prohibition of audio capability and audio queue use.

PostgreSQL integration tests cover:

- migration and backfill;
- uniqueness and primary-source constraints;
- rollback on partial dual-write failure;
- linked-source reads with legacy projection;
- durable-job idempotency under workload routing;
- RLS and public projection boundaries.

Every implementation PR must also pass:

- existing provider fixture tests;
- durable job, retry, dead-letter, stale-claim, and redrive tests;
- full API pytest against PostgreSQL and Redis;
- Python `compileall`;
- full web Vitest suite;
- Nuxt typecheck;
- Nuxt production build.

## 16. Rollout and rollback

Implementation lands in five bounded PRs:

1. registry foundation;
2. additive provider-source schema and backfill;
3. transactional dual-write and API compatibility;
4. adapter and workload-queue migration;
5. descriptor-driven health, search profiles, and generic scheduling.

Schema deployment order:

1. apply additive migration;
2. verify backfill counts and integrity queries;
3. deploy dual-write API and workers;
4. verify matching legacy and linked sources;
5. switch scheduler enqueue targets;
6. retain queue aliases through the compatibility window.

Application rollback returns to the previous image while leaving additive tables intact. It must not drop provider tables, delete source links, truncate jobs, remove PostgreSQL history, or remove Redis AOF data. After production writes use the new tables, corrective forward migrations are preferred over destructive schema rollback.

## 17. Pull-request boundaries

### PR 1 — Registry foundation

Capability contracts, descriptors, startup validation, current-provider wrappers, and fixture-provider extension tests. No schema or routing changes.

### PR 2 — Provider source schema

Tables, built-in rows, deterministic backfill, integrity constraints, indexes, and RLS tests.

### PR 3 — Dual-write compatibility

Repository interfaces, transactional dual-write, legacy projection, mismatch detection, and API compatibility tests.

### PR 4 — Adapter and queue migration

Registry adapter resolution, workload jobs and workers, temporary queue aliases, normalized errors, and explicit prohibition of audio tasks.

### PR 5 — Descriptor-driven operations

Generic scheduling, search-profile schemas, provider health, operator compatibility, and completion evidence for #19 plus applicable work from #7.

## 18. Exit criteria

v0.3 is complete only when:

1. a fixture provider is added through registration and tests without editing a closed enum or central dispatch branch;
2. every existing set has exactly one valid backfilled primary source link;
3. every current import path dual-writes consistent legacy and provider-link data;
4. YouTube, SoundCloud, and FTM run through the registry;
5. scheduler and workers route by workload class;
6. health and search-profile validation are descriptor-driven;
7. existing API responses remain compatible;
8. no media acquisition or audio storage is enabled;
9. full CI passes on the exact release commit;
10. rollback instructions are verified against the additive strategy.
