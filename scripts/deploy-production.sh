#!/usr/bin/env bash
set -Eeuo pipefail

compose_file="${COMPOSE_FILE:-docker-compose.production.yml}"
env_file="${ENV_FILE:-.env.production}"
api_domain="${1:-api.syco23.org}"
health_timeout_seconds="${HEALTH_TIMEOUT_SECONDS:-240}"
beat_stability_seconds="${BEAT_STABILITY_SECONDS:-20}"
provider_activation_ack="${PROVIDER_ACTIVATION_ACK:-}"

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

fail() {
  printf '\n[deploy:error] %s\n' "$*" >&2
  if [[ -f "$compose_file" && -f "$env_file" ]]; then
    compose ps >&2 || true
    compose logs --tail=120 api worker-beat caddy >&2 || true
  fi
  exit 1
}

env_value() {
  local key="$1"
  local value
  value="$(
    awk -v key="$key" '
      $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
        sub(/^[^=]*=/, "")
        print
      }
    ' "$env_file" | tail -n 1
  )"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

require_env_value() {
  local key="$1"
  [[ -n "$(env_value "$key")" ]] || fail "$key is required for live provider activation"
}

require_env_true() {
  local key="$1"
  [[ "$(env_value "$key")" == "true" ]] || fail "$key must be true for all-provider activation"
}

trap 'fail "deployment stopped near line $LINENO"' ERR

[[ -f "$compose_file" ]] || fail "missing $compose_file"
[[ -f "$env_file" ]] || fail "missing $env_file; copy .env.production.example and fill host-only secrets"
[[ "$api_domain" =~ ^[A-Za-z0-9.-]+$ ]] || fail "invalid API domain"
[[ "$health_timeout_seconds" =~ ^[0-9]+$ ]] || fail "HEALTH_TIMEOUT_SECONDS must be an integer"
[[ "$beat_stability_seconds" =~ ^[0-9]+$ ]] || fail "BEAT_STABILITY_SECONDS must be an integer"
command -v docker >/dev/null 2>&1 || fail "docker is not installed"
command -v curl >/dev/null 2>&1 || fail "curl is not installed"

docker info >/dev/null 2>&1 || fail "docker daemon is unavailable"
chmod 600 "$env_file"

provider_mode="$(env_value PROVIDER_MODE)"
case "$provider_mode" in
  fixture)
    ;;
  live)
    [[ "$provider_activation_ack" == "activate-all" ]] || fail \
      "live provider activation requires PROVIDER_ACTIVATION_ACK=activate-all"
    require_env_value YOUTUBE_API_KEY
    require_env_true ARCHIVE_ORG_ENABLED
    require_env_true MIXCLOUD_ENABLED
    require_env_true AUDIUS_ENABLED
    require_env_value AUDIUS_API_BEARER_TOKEN
    require_env_true RSS_ENABLED
    require_env_value RSS_TRUSTED_FEEDS_JSON
    require_env_true FTM_SCRAPER_ENABLED

    rss_trusted_feeds_json="$(env_value RSS_TRUSTED_FEEDS_JSON)"
    rss_compact="$(printf '%s' "$rss_trusted_feeds_json" | tr -d '[:space:]')"
    [[ "$rss_compact" == \{*\} && "$rss_compact" != "{}" ]] || fail \
      "RSS_TRUSTED_FEEDS_JSON must contain at least one trusted feed"
    ;;
  *)
    fail 'PROVIDER_MODE must be "fixture" or "live"'
    ;;
esac

if ss -lnt 2>/dev/null | awk '{print $4}' | grep -Eq '(^|:)(80|443)$'; then
  running_caddy_id="$(compose ps -q caddy 2>/dev/null || true)"
  if [[ -z "$running_caddy_id" ]]; then
    fail "port 80 or 443 is already occupied by a service outside this compose project"
  fi
fi

mkdir -p .deployments
deploy_id="$(date -u +%Y%m%dT%H%M%SZ)"
commit="$(git rev-parse --short=12 HEAD 2>/dev/null || printf 'unknown')"
record=".deployments/${deploy_id}.txt"
{
  printf 'deployment_id=%s\n' "$deploy_id"
  printf 'commit=%s\n' "$commit"
  printf 'api_domain=%s\n' "$api_domain"
  printf 'provider_mode=%s\n' "$provider_mode"
  printf 'started_at=%s\n' "$(date -u +%FT%TZ)"
} > "$record"

printf '[deploy] validating compose configuration\n'
compose config --quiet

printf '[deploy] building immutable API and worker images\n'
compose build --pull api worker-provider-api worker-provider-scrape worker-process worker-beat

printf '[deploy] starting isolated SETCRAWLER services\n'
compose up -d --remove-orphans

api_id="$(compose ps -q api)"
[[ -n "$api_id" ]] || fail "api container was not created"

deadline=$((SECONDS + health_timeout_seconds))
printf '[deploy] waiting for API container health\n'
while (( SECONDS < deadline )); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$api_id" 2>/dev/null || true)"
  case "$health" in
    healthy) break ;;
    unhealthy|exited|dead) fail "api container entered terminal state: $health" ;;
  esac
  sleep 5
done

health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$api_id" 2>/dev/null || true)"
[[ "$health" == "healthy" ]] || fail "api container did not become healthy within ${health_timeout_seconds}s"

if [[ "$provider_mode" == "live" ]]; then
  printf '[deploy] verifying effective provider activation\n'
  compose exec -T api python - <<'PY'
import psycopg

from app.core.config import get_settings
from app.services.provider import build_provider_registry
from app.services.provider_health import provider_runtime_states

settings = get_settings()
registry = build_provider_registry(settings)
states = provider_runtime_states(registry, settings)
disabled_runtime = sorted(
    key for key, state in states.items() if not state["effective_enabled"]
)
if disabled_runtime:
    raise SystemExit(
        "providers not effectively enabled by runtime configuration: "
        + ", ".join(disabled_runtime)
    )

with psycopg.connect(settings.database_url) as connection:
    rows = connection.execute(
        "select key, enabled from public.providers order by key"
    ).fetchall()

database_state = {str(key): bool(enabled) for key, enabled in rows}
missing_database = sorted(set(states) - set(database_state))
disabled_database = sorted(
    key for key in states if database_state.get(key) is not True
)
if missing_database or disabled_database:
    details = []
    if missing_database:
        details.append("missing=" + ",".join(missing_database))
    if disabled_database:
        details.append("disabled=" + ",".join(disabled_database))
    raise SystemExit("provider database activation incomplete: " + "; ".join(details))

print(
    "all configured providers are effectively enabled: "
    + ", ".join(sorted(states))
)
PY
fi

beat_id="$(compose ps -q worker-beat)"
[[ -n "$beat_id" ]] || fail "worker-beat container was not created"
beat_restart_count="$(docker inspect --format '{{.RestartCount}}' "$beat_id" 2>/dev/null || true)"
[[ "$beat_restart_count" =~ ^[0-9]+$ ]] || fail "could not read worker-beat restart count"
[[ "$beat_restart_count" == "0" ]] || fail "worker-beat already restarted ${beat_restart_count} time(s)"

printf '[deploy] checking worker-beat stability for %ss\n' "$beat_stability_seconds"
beat_deadline=$((SECONDS + beat_stability_seconds))
while (( SECONDS < beat_deadline )); do
  beat_state="$(docker inspect --format '{{.State.Status}}' "$beat_id" 2>/dev/null || true)"
  current_restart_count="$(docker inspect --format '{{.RestartCount}}' "$beat_id" 2>/dev/null || true)"
  [[ "$beat_state" == "running" ]] || fail "worker-beat entered terminal state: $beat_state"
  [[ "$current_restart_count" == "$beat_restart_count" ]] || fail "worker-beat restarted during the stability window"
  sleep 2
done

printf '[deploy] waiting for public HTTPS health at https://%s/health\n' "$api_domain"
public_deadline=$((SECONDS + health_timeout_seconds))
health_body=""
while (( SECONDS < public_deadline )); do
  if health_body="$(curl --fail --silent --show-error --max-time 10 "https://${api_domain}/health" 2>/dev/null)"; then
    break
  fi
  sleep 5
done

[[ "$health_body" == *'"status":"ok"'* ]] || fail "public health endpoint did not return status ok"
[[ "$health_body" == *'"service":"syco23-setcrawler-api"'* ]] || fail "public health endpoint returned an unexpected service"

{
  printf 'completed_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'health=%s\n' "$health_body"
} >> "$record"

printf '\n[deploy] production stack is healthy\n'
printf '[deploy] commit: %s\n' "$commit"
printf '[deploy] provider mode: %s\n' "$provider_mode"
printf '[deploy] health: %s\n' "$health_body"
printf '[deploy] record: %s\n\n' "$record"
compose ps
