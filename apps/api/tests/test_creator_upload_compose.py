from pathlib import Path


def test_local_compose_mounts_creator_upload_migration_after_audio_jobs() -> None:
    compose = Path(__file__).parents[3] / "docker-compose.yml"
    text = compose.read_text()

    audio_jobs = "20260802030000_audio_acquisition_jobs.sql"
    creator_uploads = "20260802120000_creator_upload_sessions.sql"

    assert audio_jobs in text
    assert creator_uploads in text
    assert text.index(audio_jobs) < text.index(creator_uploads)
