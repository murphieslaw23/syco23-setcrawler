import json

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repository import InMemoryRepository
from app.schemas import JobStatus, JobType, SetSource
from app.schemas.import_job import ImportJobPatch
from conftest import RecordingDispatcher


def make_client() -> TestClient:
    return TestClient(
        create_app(
            InMemoryRepository.seeded(),
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
            ),
            dispatcher=RecordingDispatcher(),
        )
    )


def test_health_and_stats_contract() -> None:
    client = make_client()

    assert client.get("/health").json() == {"status": "ok", "service": "syco23-setcrawler-api"}
    stats = client.get("/stats").json()

    assert stats["total_sets"] == 6
    assert stats["by_status"]["inbox"] == 4
    assert stats["queue"] == {
        "queued": 0,
        "processing": 0,
        "failed": 0,
        "completed": 0,
        "retry": 0,
        "blocked": 0,
    }


def test_provider_health_redacts_secrets() -> None:
    """Operational health must expose capability flags without credentials."""
    client = TestClient(
        create_app(
            InMemoryRepository.seeded(),
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
                youtube_api_key="secret-youtube-key",
                yt_dlp_bin="/private/yt-dlp",
                scraper_user_agent="syco23 (+contact: private@example.com)",
                ftm_scraper_enabled=False,
            ),
            dispatcher=RecordingDispatcher(),
        )
    )

    response = client.get("/providers")

    assert response.status_code == 200
    body = response.json()
    assert body["youtube"] == {
        "configured": True,
        "enabled": True,
        "mode": "official_api",
        "runtime_mode": "live",
    }
    assert body["soundcloud"] == {
        "configured": True,
        "enabled": True,
        "mode": "manual_url",
        "runtime_mode": "live",
    }
    assert body["freeteknomusic"] == {
        "configured": True,
        "enabled": False,
        "mode": "robots_crawl",
        "runtime_mode": "live",
    }
    serialized = json.dumps(body).casefold()
    assert "secret-youtube-key" not in serialized
    assert "/private/yt-dlp" not in serialized
    assert "private@example.com" not in serialized
    assert "api_key" not in serialized
    assert "database_url" not in serialized
    assert "yt_dlp_bin" not in serialized
    assert "scraper_user_agent" not in serialized
    assert "private" not in serialized
    assert "contact" not in serialized


def test_queue_filters_by_source_and_status_with_stable_pagination() -> None:
    """Queue filtering must use the public status name and count only matches."""
    repository = InMemoryRepository.seeded()
    matching = repository.create_job(
        url="https://soundcloud.com/syco23/failed-set",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    repository.transition_job(
        matching.id,
        ImportJobPatch(status=JobStatus.failed),
    )
    repository.create_job(
        url="https://soundcloud.com/syco23/queued-set",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    other = repository.create_job(
        url="https://www.youtube.com/watch?v=failed-set",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    repository.transition_job(
        other.id,
        ImportJobPatch(status=JobStatus.failed),
    )
    client = TestClient(create_app(repository, dispatcher=RecordingDispatcher()))

    response = client.get(
        "/imports/queue",
        params={"source": "soundcloud", "status": "failed", "limit": 1, "offset": 0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == [str(matching.id)]


def test_stats_count_persisted_queue_states() -> None:
    """Dashboard queue counts must come from durable jobs rather than seed values."""
    repository = InMemoryRepository.seeded()
    queued = repository.create_job(
        url="https://soundcloud.com/syco23/queued",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    processing = repository.create_job(
        url="https://soundcloud.com/syco23/processing",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    repository.transition_job(
        processing.id,
        ImportJobPatch(status=JobStatus.processing),
    )
    failed = repository.create_job(
        url="https://soundcloud.com/syco23/failed",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    repository.transition_job(failed.id, ImportJobPatch(status=JobStatus.failed))
    completed = repository.create_job(
        url="https://soundcloud.com/syco23/completed",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    repository.transition_job(completed.id, ImportJobPatch(status=JobStatus.processing))
    repository.transition_job(completed.id, ImportJobPatch(status=JobStatus.completed))
    client = TestClient(create_app(repository, dispatcher=RecordingDispatcher()))

    stats = client.get("/stats").json()

    assert queued.id
    assert stats["queue"] == {
        "queued": 1,
        "processing": 1,
        "failed": 1,
        "completed": 1,
        "retry": 0,
        "blocked": 0,
    }


def test_filters_review_inbox_by_source_and_min_score() -> None:
    client = make_client()

    response = client.get(
        "/sets",
        params={"status": "inbox", "source": "youtube", "min_score": 0.7},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(item["source"] == "youtube" for item in body["items"])
    assert all(item["set_score"] >= 0.7 for item in body["items"])


def test_set_detail_exposes_candidates_images_and_raw_payload() -> None:
    client = make_client()
    set_id = client.get("/sets", params={"status": "inbox"}).json()["items"][0]["id"]

    detail = client.get(f"/sets/{set_id}")

    assert detail.status_code == 200
    body = detail.json()
    assert body["candidates"]
    assert body["images"][0]["kind"] == "thumbnail"
    assert body["raw_payload"]["provider"] in {"youtube", "soundcloud", "freeteknomusic"}


def test_accepts_candidate_and_moves_set_into_reviewing() -> None:
    client = make_client()
    detail = client.get("/sets", params={"status": "inbox"}).json()["items"][0]
    full = client.get(f"/sets/{detail['id']}").json()
    candidate_id = full["candidates"][0]["id"]

    response = client.post(f"/sets/{detail['id']}/candidates/{candidate_id}/accept")

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert client.get(f"/sets/{detail['id']}").json()["review_status"] == "reviewing"


def test_publish_is_explicit_and_never_triggered_by_candidate_acceptance() -> None:
    client = make_client()
    set_id = client.get("/sets", params={"status": "inbox"}).json()["items"][0]["id"]

    accepted = client.patch(f"/sets/{set_id}", json={"review_status": "accepted"})
    published = client.post(f"/sets/{set_id}/publish")

    assert accepted.json()["review_status"] == "accepted"
    assert published.json()["review_status"] == "published"


def test_import_url_rejects_unsupported_hosts_and_queues_soundcloud() -> None:
    client = make_client()

    rejected = client.post("/imports/url", json={"url": "https://example.com/audio.mp3"})
    queued = client.post(
        "/imports/url",
        json={"url": "https://soundcloud.com/syco23/ritual-session"},
    )

    assert rejected.status_code == 400
    assert queued.status_code == 202
    assert queued.json()["source"] == "soundcloud"
    assert queued.json()["status"] == "queued"


def test_search_profile_crud_and_manual_run() -> None:
    client = make_client()

    created = client.post(
        "/search-profiles",
        json={"name": "Tribe B2B", "query": "tribe b2b liveset", "schedule_cron": "0 6 * * *"},
    )
    profile_id = created.json()["id"]
    run = client.post(f"/search-profiles/{profile_id}/run")

    assert created.status_code == 201
    assert run.status_code == 202
    assert run.json()["status"] == "queued"
