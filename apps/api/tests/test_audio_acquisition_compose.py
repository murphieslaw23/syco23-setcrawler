from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_local_compose_initializes_audio_jobs_after_rights_foundation() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    rights = (
        "20260802010000_rights_policy_foundation.sql:"
        "/docker-entrypoint-initdb.d/20260802010000-rights-policy-foundation.sql:ro"
    )
    audio = (
        "20260802030000_audio_acquisition_jobs.sql:"
        "/docker-entrypoint-initdb.d/20260802030000-audio-acquisition-jobs.sql:ro"
    )

    assert rights in compose
    assert audio in compose
    assert compose.index(rights) < compose.index(audio)
