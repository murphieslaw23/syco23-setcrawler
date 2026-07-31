## Summary

Describe the behavior changed and the user or operator outcome.

## Release evidence

- [ ] **Migration notes:** list every migration and rollout order, or state `None`.
- [ ] **Test evidence:** include the exact commands and results for affected API, web, integration, or operational checks.
- [ ] **Provider-boundary review:** confirm provider capabilities, official API/download boundaries, rate limits, and the no-stream-ripping rule remain enforced.
- [ ] **Secret scan:** confirm no bearer tokens, database URLs, service-role keys, provider credentials, MinIO credentials, or server-only values entered source, logs, fixtures, or public Nuxt configuration.
- [ ] **Rollback note:** describe the non-destructive rollback and identify state or migrations that must be preserved.
- [ ] **Public-data-leak review:** confirm public DTOs, pages, logs, and artifacts expose no raw payloads, private storage paths, object keys, rights evidence, or operator notes.
- [ ] The no-auto-publish invariant remains intact, or the PR is blocked.

## Operational impact

Describe deployment order, expected downtime, monitoring signals, and any manual verification required.

## Related issues

Closes #
