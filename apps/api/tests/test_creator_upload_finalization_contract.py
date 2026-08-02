import ast
from pathlib import Path


def test_creator_upload_finalization_remains_server_only() -> None:
    root = Path(__file__).parents[3]
    coordinator_source = (
        root / "apps/api/app/services/creator_upload_coordinator.py"
    ).read_text()
    storage_source = (
        root / "apps/api/app/services/creator_upload_storage.py"
    ).read_text()

    coordinator = coordinator_source.casefold()
    storage = storage_source.casefold()
    tree = ast.parse(coordinator_source)
    referenced_names = {
        node.id.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    referenced_attributes = {
        node.attr.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }

    assert "class creatoruploadcompletion" in coordinator
    assert "def complete(" in coordinator
    assert "delete_completed" in referenced_attributes
    assert "nosuchupload" in storage
    assert "public_url" not in referenced_names | referenced_attributes
    assert "presigned_url" not in referenced_names | referenced_attributes
    assert "apirouter" not in referenced_names | referenced_attributes
