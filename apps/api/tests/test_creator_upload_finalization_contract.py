from pathlib import Path


def test_creator_upload_finalization_remains_server_only() -> None:
    root = Path(__file__).parents[3]
    coordinator = (
        root / "apps/api/app/services/creator_upload_coordinator.py"
    ).read_text().casefold()
    storage = (
        root / "apps/api/app/services/creator_upload_storage.py"
    ).read_text().casefold()

    assert "class creatoruploadcompletion" in coordinator
    assert "def complete(" in coordinator
    assert "delete_completed" in coordinator
    assert "nosuchupload" in storage
    assert "presigned" not in coordinator
    assert "public_url" not in coordinator
    assert "router" not in coordinator
