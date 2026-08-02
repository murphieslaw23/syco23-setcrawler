import ast
from pathlib import Path


def _symbols(source: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(source)
    names = {
        node.id.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    attributes = {
        node.attr.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    return names, attributes


def test_creator_upload_finalization_remains_single_layer_and_server_only() -> None:
    root = Path(__file__).parents[3]
    coordinator_source = (
        root / "apps/api/app/services/creator_upload_coordinator.py"
    ).read_text()
    finalizer_source = (
        root / "apps/api/app/services/creator_upload_finalization.py"
    ).read_text()
    storage_source = (
        root / "apps/api/app/services/creator_upload_storage.py"
    ).read_text()

    coordinator = coordinator_source.casefold()
    finalizer = finalizer_source.casefold()
    storage = storage_source.casefold()
    finalizer_names, finalizer_attributes = _symbols(finalizer_source)
    coordinator_names, coordinator_attributes = _symbols(coordinator_source)

    assert "class creatoruploadfinalizer" in finalizer
    assert "class creatoruploadcompletionreceipt" in finalizer
    assert "def finalize(" in finalizer
    assert "class creatoruploadcompletion" not in coordinator
    assert "def complete(" not in coordinator
    assert "nosuchupload" in storage

    exposed_symbols = (
        finalizer_names
        | finalizer_attributes
        | coordinator_names
        | coordinator_attributes
    )
    assert "public_url" not in exposed_symbols
    assert "presigned_url" not in exposed_symbols
    assert "apirouter" not in exposed_symbols
