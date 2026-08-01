from pathlib import Path


ROOT = Path(__file__).parents[3]


def test_provider_smoke_is_manual_protected_and_acquisition_free() -> None:
    workflow = (ROOT / ".github/workflows/provider-smoke.yml").read_text()
    smoke_test = (Path(__file__).parent / "test_provider_live_smoke.py").read_text()

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "schedule:" not in workflow
    assert "environment: provider-smoke" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "provider: [archive-org, mixcloud, audius, rss]" in workflow
    assert "PROVIDER_SMOKE_GUARD" in workflow
    assert "tests/test_provider_live_smoke.py" in workflow
    assert "apps/api/app.repositories" not in smoke_test
    assert "app.workers" not in smoke_test
    assert "fetch_authorized_audio(" not in smoke_test
    assert "download_candidates" in smoke_test
