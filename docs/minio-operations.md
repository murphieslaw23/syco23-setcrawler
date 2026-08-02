# Private MinIO audio storage operations

## Scope

SETCRAWLER provisions one internal MinIO service for authorized audio objects. The service is **not exposed** through host ports, Caddy, the public API, or the Nuxt application. This storage layer does not authorize acquisition or publication; rights decisions remain in PostgreSQL and are enforced by later workflow stages.

The fixed private buckets are:

- `audio-quarantine` — newly received authorized files awaiting review.
- `audio-originals` — approved, preserved source files.
- `audio-derivatives` — approved processing outputs.

The `audio-storage-init` one-shot container creates missing buckets and removes bucket policies so anonymous access remains disabled. Object keys are opaque server-generated identifiers under `objects/<prefix>/<uuid>`; operators and clients must never derive keys from filenames, set titles, provider IDs, or user input.

## Credentials and network boundary

- Generate independent, high-entropy `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` values for each deployment.
- Root credentials are used only by the initialization container. Before enabling application storage, provision a dedicated least-privilege `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` for the backend.
- Keep `AUDIO_STORAGE_ENABLED=false` until an authorized acquisition or creator-upload workflow is deployed.
- Do not add `9000` or a MinIO console port to host `ports`.
- Do not add a MinIO upstream to `docker/Caddyfile`.
- The internal Compose network uses `MINIO_ENDPOINT=minio:9000` and `MINIO_SECURE=false`; external deployments require TLS and `MINIO_SECURE=true`.

## Integrity and bounds

Every write declares an exact byte length and is limited by `AUDIO_MAX_OBJECT_BYTES`. The backend streams data directly to MinIO using multipart uploads and computes SHA-256 while reading. When an expected checksum is supplied, a mismatch deletes the just-written object and fails the operation. Range reads are bounded, validate opaque keys, and always release the underlying HTTP connection.

Promotion from quarantine is a server-side copy into a new opaque key. Promotion does not delete the source automatically; the calling transaction must record the destination version before deleting or expiring the quarantine object.

## Startup and verification

```bash
docker compose up -d minio
docker compose run --rm audio-storage-init
docker compose ps minio
```

Expected initializer output names all three buckets. Verify that the host has no MinIO listener and that Caddy only routes the API. Do not paste credentials, object keys, or private asset metadata into release evidence.

## Backup, restore, and accepted risk

The `minio_data` volume is persistent but v1.0 intentionally has no second audio copy. This is an accepted durability risk and must appear in release records and incident reviews.

Before destructive maintenance:

1. Stop acquisition and processing jobs.
2. Snapshot or copy the `minio_data` volume at the storage layer.
3. Export PostgreSQL rows for rights reviews, audio assets, versions, checksums, and decision events.
4. Record the exact application commit and MinIO image tag.

To restore:

1. Restore the PostgreSQL backup and `minio_data` volume from the same recovery point.
2. Start MinIO without exposing ports.
3. Run `audio-storage-init`; it is idempotent and does not delete objects.
4. Compare database size and SHA-256 checksum records with MinIO object metadata.
5. Keep acquisition disabled until integrity sampling and rights-state reconciliation succeed.

Deleting an object is irreversible without a volume backup. An expired or rejected object must retain its database audit tombstone even after its private bytes are removed.
