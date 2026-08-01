from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_release_status_names_every_v021_gate() -> None:
    text = (ROOT / "docs" / "release-status.md").read_text()

    for gate in ("CI runner", "JWT role matrix", "provider smoke", "observability"):
        assert gate in text
    for issue in ("#2", "#4", "#5", "#6", "#12"):
        assert issue in text
    assert "v0.2.1 must not be tagged" in text


def test_compose_exposes_the_provider_worker_contract() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()

    for worker, command in {
        "worker-provider-api": (
            "celery -A app.workers.celery_app:celery_app worker "
            "-Q provider-api,youtube --concurrency=1 --loglevel=INFO"
        ),
        "worker-provider-scrape": (
            "celery -A app.workers.celery_app:celery_app worker "
            "-Q provider-scrape,soundcloud,ftm --concurrency=1 --loglevel=INFO"
        ),
        "worker-process": (
            "celery -A app.workers.celery_app:celery_app worker "
            "-Q process --concurrency=2 --loglevel=INFO"
        ),
    }.items():
        worker_block = compose.split(f"  {worker}:\n", maxsplit=1)[1]
        assert f"command: {command}" in worker_block
        assert "DATABASE_URL: postgresql://postgres:postgres@db:5432/syco23" in worker_block
        assert "REDIS_URL: redis://redis:6379/0" in worker_block
        assert "db:\n        condition: service_healthy" in worker_block
        assert "redis:\n        condition: service_healthy" in worker_block

    api = compose.split("\n  api:\n", maxsplit=1)[1].split(
        "\n  db:\n", maxsplit=1
    )[0]
    for expected in (
        'YOUTUBE_API_KEY: "${YOUTUBE_API_KEY:-}"',
        'SCRAPER_USER_AGENT: "${SCRAPER_USER_AGENT:-syco23-setcrawler/0.1 (+contact: local@example.com)}"',
        'SCRAPER_REQUEST_DELAY_MS: "${SCRAPER_REQUEST_DELAY_MS:-5000}"',
        'FTM_SCRAPER_ENABLED: "${FTM_SCRAPER_ENABLED:-false}"',
        'FTM_MAX_PAGES_PER_RUN: "${FTM_MAX_PAGES_PER_RUN:-25}"',
        'PROVIDER_MODE: "${PROVIDER_MODE:-fixture}"',
        'PROVIDER_REQUEST_TIMEOUT_SECONDS: "${PROVIDER_REQUEST_TIMEOUT_SECONDS:-20}"',
        'PROVIDER_OUTPUT_LIMIT_BYTES: "${PROVIDER_OUTPUT_LIMIT_BYTES:-1048576}"',
        'YT_DLP_BIN: "${YT_DLP_BIN:-yt-dlp}"',
        'ARCHIVE_ORG_ENABLED: "${ARCHIVE_ORG_ENABLED:-false}"',
        'MIXCLOUD_ENABLED: "${MIXCLOUD_ENABLED:-false}"',
        'AUDIUS_ENABLED: "${AUDIUS_ENABLED:-false}"',
        'AUDIUS_API_BEARER_TOKEN: "${AUDIUS_API_BEARER_TOKEN:-}"',
        'RSS_ENABLED: "${RSS_ENABLED:-false}"',
        'RSS_TRUSTED_FEEDS_JSON: "${RSS_TRUSTED_FEEDS_JSON:-}"',
    ):
        assert expected in api

    provider_api = compose.split("  worker-provider-api:\n", maxsplit=1)[1].split(
        "  worker-provider-scrape:\n", maxsplit=1
    )[0]
    assert 'YOUTUBE_API_KEY: "${YOUTUBE_API_KEY:-}"' in provider_api
    assert 'PROVIDER_MODE: "${PROVIDER_MODE:-fixture}"' in provider_api
    for expected in (
        'ARCHIVE_ORG_ENABLED: "${ARCHIVE_ORG_ENABLED:-false}"',
        'MIXCLOUD_ENABLED: "${MIXCLOUD_ENABLED:-false}"',
        'AUDIUS_ENABLED: "${AUDIUS_ENABLED:-false}"',
        'AUDIUS_API_BEARER_TOKEN: "${AUDIUS_API_BEARER_TOKEN:-}"',
        'RSS_ENABLED: "${RSS_ENABLED:-false}"',
        'RSS_TRUSTED_FEEDS_JSON: "${RSS_TRUSTED_FEEDS_JSON:-}"',
    ):
        assert expected in provider_api
    assert (
        'PROVIDER_REQUEST_TIMEOUT_SECONDS: '
        '"${PROVIDER_REQUEST_TIMEOUT_SECONDS:-20}"'
    ) in provider_api

    provider_scrape = compose.split("  worker-provider-scrape:\n", maxsplit=1)[1].split(
        "  worker-process:\n", maxsplit=1
    )[0]
    for expected in (
        'SCRAPER_USER_AGENT: "${SCRAPER_USER_AGENT:-syco23-setcrawler/0.1 (+contact: local@example.com)}"',
        'SCRAPER_REQUEST_DELAY_MS: "${SCRAPER_REQUEST_DELAY_MS:-5000}"',
        'FTM_SCRAPER_ENABLED: "${FTM_SCRAPER_ENABLED:-false}"',
        'FTM_MAX_PAGES_PER_RUN: "${FTM_MAX_PAGES_PER_RUN:-25}"',
        'PROVIDER_REQUEST_TIMEOUT_SECONDS: "${PROVIDER_REQUEST_TIMEOUT_SECONDS:-20}"',
        'PROVIDER_OUTPUT_LIMIT_BYTES: "${PROVIDER_OUTPUT_LIMIT_BYTES:-1048576}"',
        'PROVIDER_MODE: "${PROVIDER_MODE:-fixture}"',
        'YT_DLP_BIN: "${YT_DLP_BIN:-yt-dlp}"',
        "read_only: true",
        "/tmp:size=64m,noexec,nosuid",
        "cpus: 1.0",
        "mem_limit: 512m",
    ):
        assert expected in provider_scrape

    process_worker = compose.split("  worker-process:\n", maxsplit=1)[1].split(
        "  worker-beat:\n", maxsplit=1
    )[0]
    for expected in (
        'ARCHIVE_ORG_ENABLED: "${ARCHIVE_ORG_ENABLED:-false}"',
        'MIXCLOUD_ENABLED: "${MIXCLOUD_ENABLED:-false}"',
        'AUDIUS_ENABLED: "${AUDIUS_ENABLED:-false}"',
        'AUDIUS_API_BEARER_TOKEN: "${AUDIUS_API_BEARER_TOKEN:-}"',
        'RSS_ENABLED: "${RSS_ENABLED:-false}"',
        'RSS_TRUSTED_FEEDS_JSON: "${RSS_TRUSTED_FEEDS_JSON:-}"',
    ):
        assert expected in process_worker

    assert "worker-youtube:" not in compose
    assert "worker-soundcloud:" not in compose
    assert "worker-ftm:" not in compose
    assert "-Q audio" not in compose
    assert '"3000:3000"' in compose
    assert '"8000:8000"' in compose
    init_mounts = (
        "000-supabase-compat.sql:/docker-entrypoint-initdb.d/000-supabase-compat.sql:ro",
        "0001_init.sql:/docker-entrypoint-initdb.d/001-init.sql:ro",
        "0003_indexes.sql:/docker-entrypoint-initdb.d/003-indexes.sql:ro",
        "20260728192205_provider_jobs.sql:/docker-entrypoint-initdb.d/20260728192205-provider-jobs.sql:ro",
        "20260729060000_final_release_fixes.sql:/docker-entrypoint-initdb.d/20260729060000-final-release-fixes.sql:ro",
        "20260731110000_provider_source_schema.sql:/docker-entrypoint-initdb.d/20260731110000-provider-source-schema.sql:ro",
        "20260801120000_provider_profile_scheduling.sql:/docker-entrypoint-initdb.d/20260801120000-provider-profile-scheduling.sql:ro",
        "20260801150000_scheduler_hardening.sql:/docker-entrypoint-initdb.d/20260801150000-scheduler-hardening.sql:ro",
        "20260801210000_provider_discovery_runtime.sql:/docker-entrypoint-initdb.d/20260801210000-provider-discovery-runtime.sql:ro",
    )
    assert all(item in compose for item in init_mounts)
    assert [compose.index(item) for item in init_mounts] == sorted(
        compose.index(item) for item in init_mounts
    )
    assert "0002_rls.sql:/docker-entrypoint-initdb.d" not in compose


def test_ci_bootstraps_services_and_applies_all_migrations() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    for expected in (
        "image: postgres:16-alpine",
        "image: redis:7-alpine",
        "create role anon nologin",
        "create role authenticated nologin",
        "create or replace function auth.uid()",
        "for migration in ../../supabase/migrations/*.sql; do",
        "TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/syco23",
        "python -m compileall app",
        "npm ci",
        "npm test",
        "npm run typecheck",
        "npm run build",
    ):
        assert expected in workflow


def test_compose_persists_redis_and_runs_database_redriver() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "redis-server --appendonly yes" in compose
    assert "redis_data:/data" in compose
    assert "worker-beat:" in compose
    assert "celery -A app.workers.celery_app:celery_app beat" in compose
    assert "\n  redis_data:" in compose


def test_production_compose_keeps_state_and_secrets_on_the_backend_host() -> None:
    compose_path = ROOT / "docker-compose.production.yml"
    compose = yaml.safe_load(compose_path.read_text())
    services = compose["services"]

    assert set(services) == {
        "api",
        "redis",
        "worker-provider-api",
        "worker-provider-scrape",
        "worker-process",
        "worker-beat",
        "caddy",
    }
    assert "db" not in services
    assert "web" not in services
    assert services["redis"]["ports"] == []
    assert "redis_data:/data" in services["redis"]["volumes"]
    assert services["redis"]["command"] == [
        "redis-server",
        "--appendonly",
        "yes",
        "--appendfsync",
        "everysec",
    ]
    assert services["api"]["expose"] == ["8000"]
    assert "ports" not in services["api"]
    assert services["caddy"]["ports"] == ["80:80", "443:443", "443:443/udp"]
    assert services["worker-beat"]["command"] == [
        "celery",
        "-A",
        "app.workers.celery_app:celery_app",
        "beat",
        "--schedule=/tmp/celerybeat-schedule",
        "--loglevel=INFO",
    ]
    assert "/tmp:size=64m,noexec,nosuid" in services["worker-beat"]["tmpfs"]
    assert sum(" beat " in " ".join(service.get("command", [])) for service in services.values()) == 1
    assert "audio" not in services

    backend_services = [
        "api",
        "worker-provider-api",
        "worker-provider-scrape",
        "worker-process",
        "worker-beat",
    ]
    for service_name in backend_services:
        environment = services[service_name]["environment"]
        assert environment["ENVIRONMENT"] == "production"
        assert environment["REPOSITORY_MODE"] == "postgres"
        assert environment["AUTH_MODE"] == "supabase"
        assert environment["DATABASE_URL"] == "${DATABASE_URL:?DATABASE_URL is required}"
        assert environment["SUPABASE_URL"] == "${SUPABASE_URL:?SUPABASE_URL is required}"
        assert (
            environment["SUPABASE_ANON_KEY"]
            == "${SUPABASE_ANON_KEY:?SUPABASE_ANON_KEY is required}"
        )
        assert environment["ARCHIVE_ORG_ENABLED"] == "${ARCHIVE_ORG_ENABLED:-false}"
        assert environment["MIXCLOUD_ENABLED"] == "${MIXCLOUD_ENABLED:-false}"
        assert environment["AUDIUS_ENABLED"] == "${AUDIUS_ENABLED:-false}"
        assert environment["RSS_ENABLED"] == "${RSS_ENABLED:-false}"

    assert "SUPABASE_SERVICE_ROLE_KEY" not in compose_path.read_text()


def test_production_tls_proxy_targets_only_the_internal_api() -> None:
    caddyfile = (ROOT / "docker" / "Caddyfile").read_text()

    assert "{$API_DOMAIN}" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert "/health" in caddyfile


def test_deploy_script_rejects_a_restarting_beat_scheduler() -> None:
    deploy_script = (ROOT / "scripts" / "deploy-production.sh").read_text()

    assert "BEAT_STABILITY_SECONDS" in deploy_script
    assert "{{.RestartCount}}" in deploy_script
    assert 'fail "worker-beat restarted during the stability window"' in deploy_script


def test_production_observability_has_bounded_logs_and_a_release_runbook() -> None:
    compose_path = ROOT / "docker-compose.production.yml"
    compose = yaml.safe_load(compose_path.read_text())

    for service in compose["services"].values():
        assert service["logging"] == {
            "driver": "local",
            "options": {"max-size": "10m", "max-file": "5"},
        }

    runbook = (ROOT / "docs" / "observability.md").read_text()
    for expected in (
        "Import latency SLO",
        "Job terminalization SLO",
        "dead_letter_growth",
        "redrive_publish_failures",
        "stuck_processing_jobs",
        "provider_quota_failures",
        "provider_robots_failures",
        "X-Request-ID",
        "Release evidence checklist",
        "Rollback",
    ):
        assert expected in runbook

    env_example = (ROOT / ".env.production.example").read_text()
    for variable in (
        "HEALTH_PROBE_CACHE_SECONDS",
        "BEAT_STALE_AFTER_SECONDS",
        "DEAD_LETTER_ALERT_THRESHOLD",
        "STUCK_JOB_ALERT_THRESHOLD",
        "REDRIVE_FAILURE_ALERT_THRESHOLD",
        "PROVIDER_FAILURE_ALERT_THRESHOLD",
    ):
        assert variable in env_example


def test_ci_governance_and_pull_request_evidence_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    template = (ROOT / ".github" / "pull_request_template.md").read_text()

    for expected in (
        "workflow_dispatch:",
        "permissions:",
        "contents: read",
        "concurrency:",
        "cancel-in-progress: true",
        "timeout-minutes: 20",
        "timeout-minutes: 15",
    ):
        assert expected in workflow

    for expected in (
        "Migration notes",
        "Test evidence",
        "Provider-boundary review",
        "Secret scan",
        "Rollback note",
        "Public-data-leak review",
    ):
        assert expected in template
