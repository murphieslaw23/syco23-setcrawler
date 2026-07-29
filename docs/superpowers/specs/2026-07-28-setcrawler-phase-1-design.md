# SYCO23 Setcrawler Phase 1 Design

## Decision

Build the approved Phase 1 scope as a local-first monorepo with a Nuxt 3 editorial UI, a FastAPI REST API, Postgres-compatible persistence, Supabase migrations, and Docker Compose. The application must start without external credentials by using deterministic seed data and an in-memory repository; setting `DATABASE_URL` enables Postgres.

## Product Surface

- Dashboard with operational counts and recent review items.
- Review inbox with source, score, and text search filters.
- Set detail with provider payload, extracted field candidates, image priority, and accept/reject/publish actions.
- Browse pages for sets, artists, events, and search profiles.
- Settings page that exposes the active heuristic thresholds and keyword groups.
- URL import form that validates supported sources and creates a queued import record without downloading media.

## Architecture

- `apps/api`: FastAPI application split into domain models, repository, services, and routers.
- `apps/web`: Nuxt application with a shared API composable, focused components, and responsive page layouts.
- `supabase/migrations`: canonical Postgres schema, indexes, roles, and RLS.
- Docker Compose provides web, API, Postgres, and Redis. Redis is included for the future worker boundary but Phase 1 uses an API-owned in-memory job queue.

The API repository is injected through FastAPI dependencies. Tests use the in-memory implementation. The Postgres implementation uses SQLAlchemy async sessions and matches the same public repository contract.

## Data Flow

1. The web app requests dashboard, inbox, or detail data from FastAPI.
2. FastAPI reads from the configured repository and returns typed Pydantic responses.
3. Review actions update status or candidate state and append an audit event.
4. URL imports validate the canonical host, record a queued job, and never invoke a downloader in Phase 1.
5. When the API is unavailable, the Nuxt composable uses the same deterministic demo records so the review interface remains locally inspectable.

## Error Handling

- Request validation returns structured `422` responses.
- Missing entities return `404`.
- Invalid review transitions and unsupported import URLs return `400`.
- The frontend shows an inline error panel and keeps the last successful state.
- Destructive actions require explicit user interaction and remain reversible at the review-status level.

## Visual System

The accepted visual reference is `generated_images/exec-eee4b2b1-0dd4-42d4-bc31-0f18d453e5c4.png`.

- Near-black and dark brown structural surfaces.
- Rust orange as the single dominant accent.
- Condensed industrial display type, readable sans body, monospaced utility text.
- Hard-edged stacked plates, subtle fasteners, vents in inactive chrome, restrained worn texture.
- Dense desktop table/list and compact mobile review plates with bottom navigation.
- No terminal, neon, cyberpunk, glossy, vinyl, or legacy-brand metaphors.

## Testing

- Unit tests: heuristic score, provider normalization, duplicate fingerprints, and metadata extraction.
- API contract tests: health, inbox filters, detail, candidate decisions, review transitions, URL validation, and stats.
- Web checks: TypeScript/Nuxt build plus Playwright browser walkthrough at desktop and mobile sizes.

## Scope Boundary

Phase 1 does not call YouTube, SoundCloud, freeteknomusic.org, yt-dlp, OCR, Supabase Storage, or external queues. It defines their stable seams and ships a working review workflow without copyrighted media downloads.
