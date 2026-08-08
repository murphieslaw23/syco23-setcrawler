from __future__ import annotations

from pathlib import Path

import yaml

from app.schemas.audio import AudioAssetRecord, AudioBucket


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "supabase/migrations/20260808020000_audio_processing_ledger.sql"


def test_processing_migration_extends_private_states_versions_and_durable_jobs() -> None:
    text = MIGRATION.read_text().casefold()

    for state in ("processing", "ready", "failed"):
        assert f"'{state}'" in text
    assert "create table public.audio_processing_jobs" in text
    assert "claim_token uuid" in text
    assert "attempt_count integer" in text
    assert "derivative_object_key text" in text
    assert "create trigger audio_assets_enqueue_processing" in text
    assert "insert into public.audio_processing_jobs" in text
    for column in (
        "codec_name",
        "format_name",
        "duration_seconds",
        "bit_rate",
        "sample_rate",
        "channels",
        "metadata_tags",
    ):
        assert column in text


def test_fresh_local_postgres_bootstraps_processing_ledger() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["db"]["volumes"]
    assert (
        "./supabase/migrations/20260808020000_audio_processing_ledger.sql:"
        "/docker-entrypoint-initdb.d/20260808020000-audio-processing-ledger.sql:ro"
        in volumes
    )


def test_processing_ready_and_failed_assets_preserve_original_bucket() -> None:
    base = dict(
        rights_review_id="00000000-0000-4000-8000-00000000e001",
        bucket_name=AudioBucket.originals,
        object_key="objects/ee/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        checksum_sha256="e" * 64,
        size_bytes=23,
        content_type="audio/flac",
        expires_at=None,
    )
    from app.schemas.audio import AudioAssetState

    for state in (
        AudioAssetState.processing,
        AudioAssetState.ready,
        AudioAssetState.failed,
    ):
        record = AudioAssetRecord(state=state, **base)
        assert record.bucket_name is AudioBucket.originals
