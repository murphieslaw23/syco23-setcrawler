from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_ci_builds_media_worker_and_verifies_ffmpeg_runtime() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]

    assert "audio-processing-image" in jobs
    steps = jobs["audio-processing-image"]["steps"]
    commands = "\n".join(
        str(step.get("run", ""))
        for step in steps
        if isinstance(step, dict)
    )

    assert "docker build -f docker/worker.Dockerfile" in commands
    assert "ffmpeg -version" in commands
    assert "ffprobe -version" in commands
    assert "libmp3lame" in commands
