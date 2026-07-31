# SYCO23 SETCRAWLER release status

## Current release state

The production-shaped v0.2 baseline is deployed, but the release is not closed.
The `v0.2.1` closeout exists to collect and verify operational evidence before
schema-heavy v1 development is promoted.

**Release rule:** v0.2.1 must not be tagged until the CI runner, JWT role matrix,
provider smoke, and observability gates are complete. Issue #4 must also match
the actual production recovery evidence before it is closed.

## Completed or prepared evidence

| Area | GitHub issue | Evidence already present |
|---|---:|---|
| Dedicated Supabase project and migrations | #2 | Dedicated project, ordered migrations, private image buckets, and admin-role provisioning are recorded. |
| Backend deployment topology | #4 | Production Compose, Caddy edge, persistent Redis AOF, isolated API/workers, and non-destructive rollback procedures are checked into the repository. |
| CI runner and workflow | #12 | Run `30599334190` passed API and web on exact head `ef13879ff07f52a176d95caa9fe2b214125f5fe1`, including migrations, tests, typecheck, and build. |

Prepared artifacts are not equivalent to a passed release gate. Live execution
evidence is required below.

## Remaining v0.2.1 gates

| Gate | GitHub issue | Required evidence | State |
|---|---:|---|---|
| CI runner | #12 | GitHub-hosted API and web jobs start, execute their first steps, and pass on the exact release commit. | Verified by run `30599334190`. |
| JWT role matrix | #2 | Real Supabase JWT checks prove viewer, editor, and admin authorization through `/auth/me` and representative protected routes. | Pending. |
| Provider smoke | #5 | Sanitized metadata-only YouTube, SoundCloud, and permitted FTM smoke results, including duplicate and below-threshold behavior. | Pending. |
| Observability | #6 | Correlated API/worker/beat logs, health coverage, dead-letter and stale-job alerts, retention, redaction, SLOs, and release runbook evidence. | Pending. |
| Deployment and recovery reconciliation | #4 | Public health, login, dashboard, inbox, search profiles, exactly one beat scheduler, and queued/retry/stale-processing recovery match production reality. | Pending final evidence. |

## Evidence handling

Attach sanitized timestamps, commit SHAs, workflow/run IDs, import-job IDs, HTTP
status summaries, and recovery outcomes to the owning issue. Never attach bearer
tokens, database URLs, provider credentials, Supabase service-role keys, MinIO
credentials, or raw private payloads.

## Tag decision

Tag `v0.2.1` only when:

1. Issues #2, #4, #5, #6, and #12 are closed or explicitly re-scoped with a
   documented non-blocking rationale.
2. The four named release gates above are complete for one exact commit.
3. The production recovery drill passes without deleting PostgreSQL history or
   the Redis AOF volume.
4. The release record links the evidence and rollback procedure.
