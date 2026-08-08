from __future__ import annotations

import json
from pathlib import Path

import pytest


SOURCE_PATH = Path("/work/input/source-audio")
OUTPUT_PATH = Path("/work/output/stream.mp3")


def _probe_payload(
    *,
    codec: str,
    bit_rate: int,
    sample_rate: int = 48_000,
    channels: int = 2,
    duration: float = 3_600.25,
    tags: dict[str, str] | None = None,
    format_name: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": codec,
                    "sample_rate": str(sample_rate),
                    "channels": channels,
                    "bit_rate": str(bit_rate),
                    "tags": tags or {},
                }
            ],
            "format": {
                "format_name": format_name or codec,
                "duration": str(duration),
                "bit_rate": str(bit_rate),
                "tags": tags or {},
            },
        }
    ).encode()


class RecordingRunner:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], float, int]] = []

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        stderr_limit_bytes: int,
    ):
        self.calls.append((tuple(argv), timeout_seconds, stderr_limit_bytes))
        if not self.results:
            raise AssertionError("unexpected media command")
        return self.results.pop(0)


def _result(*, stdout: bytes = b"", stderr: str = "", returncode: int = 0):
    from app.services.audio_processing import CommandResult

    return CommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_probe_uses_bounded_ffprobe_and_normalizes_audio_metadata() -> None:
    from app.services.audio_processing import AudioProcessingCore

    runner = RecordingRunner(
        [
            _result(
                stdout=_probe_payload(
                    codec="flac",
                    bit_rate=921_600,
                    sample_rate=48_000,
                    channels=2,
                    duration=3_600.25,
                    tags={"artist": "SYCO23", "title": "Ritual Set"},
                    format_name="flac",
                )
            )
        ]
    )
    core = AudioProcessingCore(
        runner=runner,
        ffprobe_bin="/usr/bin/ffprobe",
        ffmpeg_bin="/usr/bin/ffmpeg",
        timeout_seconds=45,
        stderr_limit_bytes=16_384,
    )

    probe = core.probe(SOURCE_PATH)

    assert probe.codec_name == "flac"
    assert probe.format_name == "flac"
    assert probe.duration_seconds == pytest.approx(3_600.25)
    assert probe.bit_rate == 921_600
    assert probe.sample_rate == 48_000
    assert probe.channels == 2
    assert probe.tags == {"artist": "SYCO23", "title": "Ritual Set"}
    assert runner.calls == [
        (
            (
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bit_rate:stream_tags=artist,title,album,date,genre:format=format_name,duration,bit_rate:format_tags=artist,title,album,date,genre",
                "-of",
                "json",
                "--",
                str(SOURCE_PATH),
            ),
            45,
            16_384,
        )
    ]


def test_good_mp3_is_reused_without_lossy_reencode() -> None:
    from app.services.audio_processing import (
        AudioProcessingAction,
        AudioProcessingCore,
    )

    runner = RecordingRunner(
        [_result(stdout=_probe_payload(codec="mp3", bit_rate=256_000))]
    )
    core = AudioProcessingCore(runner=runner)

    plan = core.plan(core.probe(SOURCE_PATH))

    assert plan.action is AudioProcessingAction.reuse_original
    assert plan.preserve_original is True
    assert len(runner.calls) == 1


def test_high_quality_non_mp3_preserves_original_and_requires_derivative() -> None:
    from app.services.audio_processing import (
        AudioProcessingAction,
        AudioProcessingCore,
    )

    runner = RecordingRunner(
        [_result(stdout=_probe_payload(codec="flac", bit_rate=1_200_000))]
    )
    core = AudioProcessingCore(runner=runner)

    plan = core.plan(core.probe(SOURCE_PATH))

    assert plan.action is AudioProcessingAction.transcode_mp3
    assert plan.preserve_original is True
    assert plan.target_bit_rate == 256_000


@pytest.mark.parametrize(
    ("codec", "bit_rate", "sample_rate"),
    (
        ("aac", 320_000, 48_000),
        ("opus", 192_000, 48_000),
        ("mp3", 128_000, 44_100),
        ("mp3", 256_000, 32_000),
    ),
)
def test_nonstandard_inputs_require_verified_256k_mp3_derivative(
    codec: str,
    bit_rate: int,
    sample_rate: int,
) -> None:
    from app.services.audio_processing import (
        AudioProcessingAction,
        AudioProcessingCore,
    )

    runner = RecordingRunner(
        [
            _result(
                stdout=_probe_payload(
                    codec=codec,
                    bit_rate=bit_rate,
                    sample_rate=sample_rate,
                )
            )
        ]
    )
    core = AudioProcessingCore(runner=runner)

    plan = core.plan(core.probe(SOURCE_PATH))

    assert plan.action is AudioProcessingAction.transcode_mp3
    assert plan.target_bit_rate == 256_000
    assert plan.preserve_original is True


def test_transcode_uses_array_command_and_verifies_256k_mp3_output() -> None:
    from app.services.audio_processing import AudioProcessingCore

    tags = {"artist": "SYCO23", "title": "System Corrupt"}
    source_probe = _probe_payload(
        codec="flac",
        bit_rate=1_100_000,
        duration=3_000.0,
        tags=tags,
    )
    verified_probe = _probe_payload(
        codec="mp3",
        bit_rate=256_000,
        sample_rate=48_000,
        channels=2,
        duration=3_000.01,
        tags=tags,
        format_name="mp3",
    )
    runner = RecordingRunner(
        [
            _result(stdout=source_probe),
            _result(),
            _result(stdout=verified_probe),
        ]
    )
    core = AudioProcessingCore(
        runner=runner,
        ffprobe_bin="ffprobe",
        ffmpeg_bin="ffmpeg",
        timeout_seconds=120,
        stderr_limit_bytes=32_768,
    )
    source = core.probe(SOURCE_PATH)

    output = core.transcode_verified(
        SOURCE_PATH,
        OUTPUT_PATH,
        source_probe=source,
    )

    assert output.codec_name == "mp3"
    assert output.bit_rate == 256_000
    assert output.tags["artist"] == "SYCO23"
    assert output.tags["title"] == "System Corrupt"
    assert runner.calls[1] == (
        (
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(SOURCE_PATH),
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "0",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "256k",
            "-id3v2_version",
            "3",
            str(OUTPUT_PATH),
        ),
        120,
        32_768,
    )
    assert runner.calls[2][0][-1] == str(OUTPUT_PATH)


def test_transcode_rejects_output_with_wrong_codec_bitrate_or_tags() -> None:
    from app.services.audio_processing import (
        AudioProcessingCore,
        AudioVerificationError,
    )

    source = _probe_payload(
        codec="flac",
        bit_rate=1_000_000,
        duration=1_800,
        tags={"artist": "SYCO23", "title": "Original"},
    )
    bad_output = _probe_payload(
        codec="mp3",
        bit_rate=192_000,
        duration=1_800,
        tags={"artist": "SYCO23", "title": "Wrong"},
        format_name="mp3",
    )
    runner = RecordingRunner(
        [_result(stdout=source), _result(), _result(stdout=bad_output)]
    )
    core = AudioProcessingCore(runner=runner)
    source_probe = core.probe(SOURCE_PATH)

    with pytest.raises(AudioVerificationError):
        core.transcode_verified(
            SOURCE_PATH,
            OUTPUT_PATH,
            source_probe=source_probe,
        )


def test_corrupt_or_non_audio_probe_fails_before_processing() -> None:
    from app.services.audio_processing import AudioProbeError, AudioProcessingCore

    for result in (
        _result(returncode=1, stderr="Invalid data found when processing input"),
        _result(stdout=b"not-json"),
        _result(stdout=json.dumps({"streams": [], "format": {}}).encode()),
    ):
        runner = RecordingRunner([result])
        core = AudioProcessingCore(runner=runner)
        with pytest.raises(AudioProbeError):
            core.probe(SOURCE_PATH)
        assert len(runner.calls) == 1


def test_default_command_runner_is_shell_free_clean_and_bounds_stderr_capture() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app/services/audio_processing.py"
    ).read_text()

    assert "subprocess.Popen(" in source
    assert "shell=True" not in source
    assert "env=_CLEAN_ENV" in source
    assert "stderr=stderr_file" in source
    assert "stderr_file.read(stderr_limit_bytes" in source
    assert "process.wait(timeout=timeout_seconds)" in source
    assert "process.kill()" in source


def test_worker_image_contains_ffmpeg_runtime_without_shelling_through_package_manager() -> None:
    root = Path(__file__).resolve().parents[3]
    dockerfile = (root / "docker/worker.Dockerfile").read_text()

    assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
