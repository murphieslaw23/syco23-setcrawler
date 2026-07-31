from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def _queue_argument(command: list[str]) -> str:
    index = command.index("-Q")
    return command[index + 1]


def test_celery_retains_legacy_aliases_for_unmigrated_producers() -> None:
    text = (ROOT / "apps/api/app/workers/celery_app.py").read_text()

    assert '"app.workers.youtube_poller.*": {"queue": "youtube"}' in text
    assert '"app.workers.soundcloud_importer.*": {"queue": "soundcloud"}' in text
    assert '"app.workers.ftm_scraper.*": {"queue": "ftm"}' in text
    assert '"app.workers.normalize_worker.*": {"queue": "process"}' in text
    assert '"queue": "audio"' not in text


def test_local_compose_uses_workload_workers_with_legacy_aliases() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    assert "worker-provider-api" in services
    assert "worker-provider-scrape" in services
    assert "worker-youtube" not in services
    assert "worker-soundcloud" not in services
    assert "worker-ftm" not in services
    assert _queue_argument(services["worker-provider-api"]["command"].split()) == (
        "provider-api,youtube"
    )
    assert _queue_argument(services["worker-provider-scrape"]["command"].split()) == (
        "provider-scrape,soundcloud,ftm"
    )
    assert "worker-audio" not in services


def test_production_compose_and_deployer_use_workload_workers() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.production.yml").read_text())
    services = compose["services"]
    deploy = (ROOT / "scripts" / "deploy-production.sh").read_text()

    assert "worker-provider-api" in services
    assert "worker-provider-scrape" in services
    assert "worker-youtube" not in services
    assert "worker-soundcloud" not in services
    assert "worker-ftm" not in services
    assert _queue_argument(services["worker-provider-api"]["command"]) == (
        "provider-api,youtube"
    )
    assert _queue_argument(services["worker-provider-scrape"]["command"]) == (
        "provider-scrape,soundcloud,ftm"
    )
    assert "worker-audio" not in services
    assert (
        "worker-provider-api worker-provider-scrape worker-process worker-beat"
        in deploy
    )
    assert "worker-youtube worker-soundcloud worker-ftm" not in deploy
