#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf '[audio-lifecycle-proof] %s\n' "$1" >&2
  exit 1
}

[[ "${AUDIO_LIFECYCLE_PROOF_ACK:-}" == "prove-v0.6" ]] \
  || fail "explicit prove-v0.6 acknowledgement is required"
[[ -n "${EXPECTED_COMMIT:-}" ]] \
  || fail "EXPECTED_COMMIT is required"
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
  || fail "EXPECTED_COMMIT must be a full Git SHA"

repo_dir="${SETCRAWLER_REPO_DIR:-/opt/syco23-setcrawler}"
cd "$repo_dir"

test -f .env.production || fail ".env.production is required"
test -z "$(git status --porcelain --untracked-files=no)" \
  || fail "tracked production checkout is not clean"

git fetch --quiet origin main
git checkout --quiet main
git pull --ff-only --quiet origin main
[[ "$(git rev-parse HEAD)" == "$EXPECTED_COMMIT" ]] \
  || fail "production checkout does not match the dispatched commit"

# Validate that the real production lifecycle overlay still resolves with the
# host's production configuration. The proof itself runs in an isolated
# Compose project and never attaches to production data or object volumes.
docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  -f docker-compose.audio-lifecycle.production.yml \
  config --quiet

project="syco23-audio-proof-${EXPECTED_COMMIT:0:12}"
proof_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
export PROOF_POSTGRES_PASSWORD="$proof_secret"
export MINIO_ROOT_USER="proof${EXPECTED_COMMIT:0:12}"
export MINIO_ROOT_PASSWORD="$proof_secret"
export MINIO_ACCESS_KEY="$MINIO_ROOT_USER"
export MINIO_SECRET_KEY="$MINIO_ROOT_PASSWORD"

compose() {
  docker compose \
    -p "$project" \
    -f docker-compose.audio-lifecycle-proof.yml \
    "$@"
}

cleanup() {
  set +e
  compose stop worker-audio-lifecycle >/dev/null 2>&1 || true
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Remove only leftovers belonging to this exact isolated proof project.
compose down -v --remove-orphans >/dev/null 2>&1 || true

compose up -d --build \
  db redis minio audio-storage-init worker-audio-lifecycle

for service in db redis minio; do
  container_id="$(compose ps -q "$service")"
  [[ -n "$container_id" ]] || fail "$service proof container is missing"
  published_ports="$(docker port "$container_id" 2>/dev/null || true)"
  [[ -z "$published_ports" ]] || fail "$service unexpectedly published a host port"
done

compose ps --status running --services \
  | grep -Fx 'worker-audio-lifecycle' >/dev/null \
  || fail "private lifecycle worker is not running"

proof_output="$(
  compose run --rm --no-deps worker-audio-lifecycle \
    python -m app.cli.prove_audio_lifecycle
)"
printf '%s\n' "$proof_output"
printf '%s\n' "$proof_output" | grep -F '"proof_passed": true' >/dev/null \
  || fail "synthetic private lifecycle proof did not pass"

compose stop worker-audio-lifecycle >/dev/null
if compose ps --status running --services | grep -Fx 'worker-audio-lifecycle' >/dev/null; then
  fail "private lifecycle worker remained active after proof"
fi

printf '[audio-lifecycle-proof] protected isolated proof passed at %s\n' \
  "$EXPECTED_COMMIT"
