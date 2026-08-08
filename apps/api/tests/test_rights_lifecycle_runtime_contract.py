from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "supabase/migrations/20260808010000_rights_lifecycle_handoff.sql"


def test_rights_lifecycle_handoff_is_a_database_transaction_invariant() -> None:
    migration = MIGRATION.read_text().casefold()

    assert "create or replace function public.enqueue_rights_lifecycle_handoff" in migration
    assert "after update of status on public.rights_reviews" in migration
    assert "insert into public.audio_asset_lifecycle_jobs" in migration
    assert "new.status not in ('approved', 'rejected')" in migration
    assert "old.status is distinct from new.status" in migration
    assert "audio-quarantine" in migration
    assert "minio" not in migration
    assert "http" not in migration


def test_local_postgres_bootstraps_rights_lifecycle_handoff() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["db"]["volumes"]

    assert (
        "./supabase/migrations/20260808010000_rights_lifecycle_handoff.sql:"
        "/docker-entrypoint-initdb.d/20260808010000-rights-lifecycle-handoff.sql:ro"
        in volumes
    )
