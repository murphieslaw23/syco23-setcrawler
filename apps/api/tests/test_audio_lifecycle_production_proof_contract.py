from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/audio-lifecycle-proof.yml"
PROOF_SCRIPT = ROOT / "scripts/verify-audio-lifecycle-production.sh"
PROOF_CLI = ROOT / "apps/api/app/cli/prove_audio_lifecycle.py"
PROOF_COMPOSE = ROOT / "docker-compose.audio-lifecycle-proof.yml"


def test_audio_lifecycle_proof_is_manual_protected_and_exact_commit() -> None:
    workflow = WORKFLOW.read_text()

    for expected in (
        "workflow_dispatch:",
        "environment: audio-lifecycle-production",
        "contents: read",
        "EXPECTED_COMMIT: ${{ github.sha }}",
        "REQUEST_ACK: ${{ inputs.acknowledgement }}",
        '[[ "$REQUEST_ACK" == "$AUDIO_LIFECYCLE_PROOF_ACK" ]]',
        "VPS_SSH_PRIVATE_KEY: ${{ secrets.VPS_SSH_PRIVATE_KEY }}",
        "VPS_KNOWN_HOSTS: ${{ secrets.VPS_KNOWN_HOSTS }}",
        "AUDIO_LIFECYCLE_PROOF_ACK: prove-v0.6",
        "scripts/verify-audio-lifecycle-production.sh",
    ):
        assert expected in workflow

    assert '[[ "${{ inputs.acknowledgement }}"' not in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "ssh-keyscan" not in workflow


def test_audio_lifecycle_proof_script_fails_closed_and_leaves_profile_disabled() -> None:
    script = PROOF_SCRIPT.read_text()

    for expected in (
        "set -Eeuo pipefail",
        '[[ "${AUDIO_LIFECYCLE_PROOF_ACK:-}" == "prove-v0.6" ]]',
        '[[ -n "${EXPECTED_COMMIT:-}" ]]',
        '[[ "$(git rev-parse HEAD)" == "$EXPECTED_COMMIT" ]]',
        "docker-compose.production.yml",
        "docker-compose.audio-lifecycle.production.yml",
        "docker-compose.audio-lifecycle-proof.yml",
        "audio-storage-init",
        "worker-audio-lifecycle",
        "python -m app.cli.prove_audio_lifecycle",
        "docker compose",
        "stop worker-audio-lifecycle",
        "down -v --remove-orphans",
    ):
        assert expected in script

    assert "worker-beat" not in script
    assert "--profile audio-lifecycle up -d worker-beat" not in script


def test_audio_lifecycle_proof_network_is_ephemeral_and_unpublished() -> None:
    compose = yaml.safe_load(PROOF_COMPOSE.read_text())
    services = compose["services"]

    assert set(services) == {
        "db",
        "redis",
        "minio",
        "audio-storage-init",
        "worker-audio-lifecycle",
    }
    for service_name in ("db", "redis", "minio"):
        assert services[service_name]["ports"] == []
    assert "api" not in services
    assert "web" not in services


def test_audio_lifecycle_proof_cli_is_synthetic_private_and_self_cleaning() -> None:
    proof = PROOF_CLI.read_text()

    for expected in (
        "AUDIO_LIFECYCLE_PROOF_ACK",
        '"prove-v0.6"',
        "audio-quarantine",
        "audio-originals",
        "execute_audio_lifecycle_jobs",
        "cleanup",
        "proof_passed",
    ):
        assert expected in proof

    for prohibited in (
        "presigned_get_object",
        "presigned_put_object",
        "localhost:9000",
        "0.0.0.0:9000",
    ):
        assert prohibited not in proof
