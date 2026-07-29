from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_compose_exposes_the_provider_worker_contract() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()

    for worker, command in {
        "worker-youtube": (
            "celery -A app.workers.celery_app:celery_app worker "
            "-Q youtube --concurrency=1 --loglevel=INFO"
        ),
        "worker-soundcloud": (
            "celery -A app.workers.celery_app:celery_app worker "
            "-Q soundcloud --concurrency=1 --loglevel=INFO"
        ),
        "worker-ftm": (
            "celery -A app.workers.celery_app:celery_app worker "
            "-Q ftm --concurrency=1 --loglevel=INFO"
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
    ):
        assert expected in api

    youtube = compose.split("  worker-youtube:\n", maxsplit=1)[1].split(
        "  worker-soundcloud:\n", maxsplit=1
    )[0]
    assert 'YOUTUBE_API_KEY: "${YOUTUBE_API_KEY:-}"' in youtube
    assert 'PROVIDER_MODE: "${PROVIDER_MODE:-fixture}"' in youtube
    assert (
        'PROVIDER_REQUEST_TIMEOUT_SECONDS: '
        '"${PROVIDER_REQUEST_TIMEOUT_SECONDS:-20}"'
    ) in youtube

    ftm = compose.split("  worker-ftm:\n", maxsplit=1)[1].split(
        "  worker-process:\n", maxsplit=1
    )[0]
    for expected in (
        'SCRAPER_USER_AGENT: "${SCRAPER_USER_AGENT:-syco23-setcrawler/0.1 (+contact: local@example.com)}"',
        'SCRAPER_REQUEST_DELAY_MS: "${SCRAPER_REQUEST_DELAY_MS:-5000}"',
        'FTM_SCRAPER_ENABLED: "${FTM_SCRAPER_ENABLED:-false}"',
        'FTM_MAX_PAGES_PER_RUN: "${FTM_MAX_PAGES_PER_RUN:-25}"',
        'PROVIDER_REQUEST_TIMEOUT_SECONDS: "${PROVIDER_REQUEST_TIMEOUT_SECONDS:-20}"',
        'PROVIDER_MODE: "${PROVIDER_MODE:-fixture}"',
    ):
        assert expected in ftm

    soundcloud = compose.split("  worker-soundcloud:\n", maxsplit=1)[1].split(
        "  worker-ftm:\n", maxsplit=1
    )[0]
    assert 'PROVIDER_MODE: "${PROVIDER_MODE:-fixture}"' in soundcloud

    soundcloud = compose.split("  worker-soundcloud:\n", maxsplit=1)[1].split(
        "  worker-ftm:\n", maxsplit=1
    )[0]
    for expected in (
        "read_only: true",
        "/tmp:size=64m,noexec,nosuid",
        "cpus: 1.0",
        "mem_limit: 512m",
    ):
        assert expected in soundcloud

    assert '"3000:3000"' in compose
    assert '"8000:8000"' in compose
    init_mounts = (
        "000-supabase-compat.sql:/docker-entrypoint-initdb.d/000-supabase-compat.sql:ro",
        "0001_init.sql:/docker-entrypoint-initdb.d/001-init.sql:ro",
        "0003_indexes.sql:/docker-entrypoint-initdb.d/003-indexes.sql:ro",
        "20260728192205_provider_jobs.sql:/docker-entrypoint-initdb.d/20260728192205-provider-jobs.sql:ro",
        "20260729060000_final_release_fixes.sql:/docker-entrypoint-initdb.d/20260729060000-final-release-fixes.sql:ro",
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
        "worker-youtube",
        "worker-soundcloud",
        "worker-ftm",
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
        "--loglevel=INFO",
    ]
    assert sum(" beat " in " ".join(service.get("command", [])) for service in services.values()) == 1

    backend_services = [
        "api",
        "worker-youtube",
        "worker-soundcloud",
        "worker-ftm",
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

    assert "SUPABASE_SERVICE_ROLE_KEY" not in compose_path.read_text()


def test_production_tls_proxy_targets_only_the_internal_api() -> None:
    caddyfile = (ROOT / "docker" / "Caddyfile").read_text()

    assert "{$API_DOMAIN}" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert "/health" in caddyfile
