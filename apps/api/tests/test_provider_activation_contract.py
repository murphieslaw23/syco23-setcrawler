from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_production_provider_mode_is_explicit() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.production.yml").read_text())

    for service_name in (
        "api",
        "worker-provider-api",
        "worker-provider-scrape",
        "worker-process",
        "worker-beat",
    ):
        environment = compose["services"][service_name]["environment"]
        assert environment["PROVIDER_MODE"] == "${PROVIDER_MODE:?PROVIDER_MODE is required}"


def test_deploy_script_has_an_explicit_all_provider_activation_gate() -> None:
    script_path = ROOT / "scripts" / "deploy-production.sh"
    script = script_path.read_text()

    subprocess.run(["bash", "-n", str(script_path)], check=True)
    assert "PROVIDER_ACTIVATION_ACK" in script
    assert "activate-all" in script
    assert 'PROVIDER_MODE must be "fixture" or "live"' in script
    for variable in (
        "YOUTUBE_API_KEY",
        "ARCHIVE_ORG_ENABLED",
        "MIXCLOUD_ENABLED",
        "AUDIUS_ENABLED",
        "AUDIUS_API_BEARER_TOKEN",
        "RSS_ENABLED",
        "RSS_TRUSTED_FEEDS_JSON",
        "FTM_SCRAPER_ENABLED",
    ):
        assert variable in script
    assert "all configured providers are effectively enabled" in script
    assert "provider_activation_ack" in script


def test_production_example_documents_live_provider_activation() -> None:
    example = (ROOT / ".env.production.example").read_text()

    assert "PROVIDER_MODE=fixture" in example
    assert "PROVIDER_MODE=live" in example
    for setting in (
        "ARCHIVE_ORG_ENABLED=true",
        "MIXCLOUD_ENABLED=true",
        "AUDIUS_ENABLED=true",
        "RSS_ENABLED=true",
        "FTM_SCRAPER_ENABLED=true",
    ):
        assert setting in example
    assert "PROVIDER_ACTIVATION_ACK=activate-all ./scripts/deploy-production.sh" in example
    assert "PROVIDER_ACTIVATION_ACK=activate-all\n" not in example
