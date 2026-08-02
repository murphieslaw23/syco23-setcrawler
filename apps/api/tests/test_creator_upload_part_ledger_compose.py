from pathlib import Path


def test_local_compose_mounts_part_ledger_after_creator_upload_sessions() -> None:
    compose = Path(__file__).parents[3] / "docker-compose.yml"
    text = compose.read_text()

    sessions = "20260802120000_creator_upload_sessions.sql"
    ledger = "20260802150000_creator_upload_part_ledger.sql"

    assert sessions in text
    assert ledger in text
    assert text.index(sessions) < text.index(ledger)
