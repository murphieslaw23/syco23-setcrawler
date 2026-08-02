from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
MINIO_RELEASE = "RELEASE.2025-10-15T17-29-55Z"
MINIO_COMMIT = "9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a"


def test_ci_builds_the_pinned_minio_server_image() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]

    assert "minio-image" in jobs
    job = jobs["minio-image"]
    assert job["timeout-minutes"] == 15

    commands = "\n".join(
        step.get("run", "")
        for step in job["steps"]
        if isinstance(step, dict)
    )
    assert "docker build" in commands
    assert "--file docker/minio.Dockerfile" in commands
    assert f"--build-arg MINIO_VERSION={MINIO_RELEASE}" in commands
    assert f"--build-arg MINIO_COMMIT={MINIO_COMMIT}" in commands
    assert "minio --version" in commands
    assert f'grep -F "{MINIO_RELEASE}"' in commands
    assert "DEVELOPMENT.GOGET" in commands
