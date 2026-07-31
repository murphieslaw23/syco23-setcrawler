from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_ci_bootstraps_all_supabase_api_roles() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    for role in ("anon", "authenticated", "service_role"):
        assert f"create role {role} nologin" in workflow
