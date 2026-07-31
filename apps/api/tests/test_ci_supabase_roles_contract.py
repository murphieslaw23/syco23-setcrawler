from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_ci_uses_complete_supabase_compatibility_bootstrap() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    compatibility = (
        ROOT / "docker" / "postgres" / "000-supabase-compat.sql"
    ).read_text()

    assert "--file ../../docker/postgres/000-supabase-compat.sql" in workflow
    for role in ("anon", "authenticated", "service_role"):
        assert f"create role {role} nologin" in compatibility
    assert "create schema if not exists storage" in compatibility
    assert "create table if not exists storage.buckets" in compatibility
