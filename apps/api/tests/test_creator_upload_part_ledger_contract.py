from importlib import import_module
from pathlib import Path


def test_creator_upload_part_ledger_repository_imports() -> None:
    repository = import_module("app.repositories.creator_upload_multipart")

    assert hasattr(repository, "InMemoryCreatorUploadMultipartRepository")
    assert hasattr(repository, "PostgresCreatorUploadMultipartRepository")


def test_local_compose_mounts_creator_upload_part_ledger_migration() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text().casefold()

    assert "20260802150000_creator_upload_part_ledger.sql" in compose
    assert "20260802150000-creator-upload-part-ledger.sql" in compose
