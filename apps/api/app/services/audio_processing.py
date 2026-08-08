from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


_CLEAN_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
_SELECTED_TAGS = ("artist", "title", "album", "date", "genre")
_STDOUT_LIMIT_BYTES = 1_048_576
_TARGET_MP3_BIT_RATE = 256_000
_MIN_REUSE_MP3_BIT_RATE = 192_000
_MIN_REUSE_SAMPLE_RATE = 44_100
_ALLOWED_CHANNEL_COUNTS = frozenset({1, 2})


class AudioProcessingError(RuntimeError):
    """Base error for private media processing."""


class AudioProbeError(AudioProcessingError):
    """Raised when ffprobe cannot produce a usable audio description."""


class AudioTranscodeError(AudioProcessingError):
    """Raised when FFmpeg cannot create a derivative."""


class AudioVerificationError(AudioProcessingError):
    """Raised when a derivative does not match the processing contract."""


class AudioProcessingAction(StrEnum):
    reuse_original = "reuse_original"
    transcode_mp3 = "transcode_mp3"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True, slots=True)
class AudioProbe:
    codec_name: str
    format_name: str
    duration_seconds: float
    bit_rate: int
    sample_rate: int
    channels: int
    tags: dict[str, str]


@dataclass(frozen=True, slots=True)
class AudioProcessingPlan:
    action: AudioProcessingAction
    preserve_original: bool
    target_bit_rate: int | None = None


class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        stderr_limit_bytes: int,
    ) -> CommandResult: ...


class BoundedCommandRunner:
    """Run media tools without a shell and with bounded captured output."""

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        stderr_limit_bytes: int,
    ) -> CommandResult:
        if not argv:
            raise ValueError("media command cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if stderr_limit_bytes < 1:
            raise ValueError("stderr_limit_bytes must be positive")

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=_CLEAN_ENV,
                close_fds=True,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                stderr_file.seek(0)
                stderr = stderr_file.read(stderr_limit_bytes + 1)
                message = stderr[:stderr_limit_bytes].decode("utf-8", errors="replace")
                raise AudioProcessingError(
                    f"media command timed out after {timeout_seconds:g}s: {message}"
                ) from error

            stdout_file.seek(0)
            stdout = stdout_file.read(_STDOUT_LIMIT_BYTES + 1)
            if len(stdout) > _STDOUT_LIMIT_BYTES:
                raise AudioProcessingError("media command stdout exceeded safety bound")

            stderr_file.seek(0)
            stderr_bytes = stderr_file.read(stderr_limit_bytes + 1)
            truncated = len(stderr_bytes) > stderr_limit_bytes
            stderr = stderr_bytes[:stderr_limit_bytes].decode(
                "utf-8",
                errors="replace",
            )
            if truncated:
                stderr = f"{stderr}\n[stderr truncated]"

            return CommandResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )


class AudioProcessingCore:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        ffprobe_bin: str = "ffprobe",
        ffmpeg_bin: str = "ffmpeg",
        timeout_seconds: float = 120,
        stderr_limit_bytes: int = 32_768,
    ) -> None:
        if not ffprobe_bin or not ffmpeg_bin:
            raise ValueError("media binary names must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if stderr_limit_bytes < 1:
            raise ValueError("stderr_limit_bytes must be positive")
        self._runner = runner or BoundedCommandRunner()
        self._ffprobe_bin = ffprobe_bin
        self._ffmpeg_bin = ffmpeg_bin
        self._timeout_seconds = timeout_seconds
        self._stderr_limit_bytes = stderr_limit_bytes

    def probe(self, path: Path) -> AudioProbe:
        result = self._runner.run(
            [
                self._ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bit_rate:stream_tags=artist,title,album,date,genre:format=format_name,duration,bit_rate:format_tags=artist,title,album,date,genre",
                "-of",
                "json",
                "--",
                str(path),
            ],
            timeout_seconds=self._timeout_seconds,
            stderr_limit_bytes=self._stderr_limit_bytes,
        )
        if result.returncode != 0:
            raise AudioProbeError(
                f"ffprobe failed with exit {result.returncode}: {result.stderr}"
            )

        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AudioProbeError("ffprobe returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AudioProbeError("ffprobe result must be an object")

        streams = payload.get("streams")
        if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
            raise AudioProbeError("ffprobe did not return an audio stream")
        stream = streams[0]
        format_payload = payload.get("format")
        if not isinstance(format_payload, dict):
            raise AudioProbeError("ffprobe did not return format metadata")

        codec_name = self._required_text(stream.get("codec_name"), "codec")
        format_name = self._required_text(
            format_payload.get("format_name"),
            "format",
        )
        duration_seconds = self._positive_float(
            format_payload.get("duration"),
            "duration",
        )
        bit_rate = self._positive_int(
            stream.get("bit_rate") or format_payload.get("bit_rate"),
            "bit rate",
        )
        sample_rate = self._positive_int(stream.get("sample_rate"), "sample rate")
        channels = self._positive_int(stream.get("channels"), "channels")

        tags: dict[str, str] = {}
        self._merge_tags(tags, format_payload.get("tags"))
        self._merge_tags(tags, stream.get("tags"))

        return AudioProbe(
            codec_name=codec_name.casefold(),
            format_name=format_name.casefold(),
            duration_seconds=duration_seconds,
            bit_rate=bit_rate,
            sample_rate=sample_rate,
            channels=channels,
            tags=tags,
        )

    def plan(self, probe: AudioProbe) -> AudioProcessingPlan:
        reusable_mp3 = (
            probe.codec_name == "mp3"
            and probe.bit_rate >= _MIN_REUSE_MP3_BIT_RATE
            and probe.sample_rate >= _MIN_REUSE_SAMPLE_RATE
            and probe.channels in _ALLOWED_CHANNEL_COUNTS
        )
        if reusable_mp3:
            return AudioProcessingPlan(
                action=AudioProcessingAction.reuse_original,
                preserve_original=True,
            )
        return AudioProcessingPlan(
            action=AudioProcessingAction.transcode_mp3,
            preserve_original=True,
            target_bit_rate=_TARGET_MP3_BIT_RATE,
        )

    def transcode_verified(
        self,
        source_path: Path,
        output_path: Path,
        *,
        source_probe: AudioProbe,
    ) -> AudioProbe:
        result = self._runner.run(
            [
                self._ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
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
                str(output_path),
            ],
            timeout_seconds=self._timeout_seconds,
            stderr_limit_bytes=self._stderr_limit_bytes,
        )
        if result.returncode != 0:
            raise AudioTranscodeError(
                f"ffmpeg failed with exit {result.returncode}: {result.stderr}"
            )

        output_probe = self.probe(output_path)
        self._verify_derivative(source_probe, output_probe)
        return output_probe

    @staticmethod
    def _verify_derivative(source: AudioProbe, output: AudioProbe) -> None:
        if output.codec_name != "mp3":
            raise AudioVerificationError("derivative codec is not MP3")
        if not 248_000 <= output.bit_rate <= 264_000:
            raise AudioVerificationError("derivative bitrate is not 256 kbit/s CBR")
        if output.sample_rate <= 0:
            raise AudioVerificationError("derivative sample rate is invalid")
        if output.channels not in _ALLOWED_CHANNEL_COUNTS:
            raise AudioVerificationError("derivative channel count is invalid")
        if output.channels != source.channels:
            raise AudioVerificationError("derivative channel count changed")

        duration_tolerance = max(1.0, source.duration_seconds * 0.001)
        if abs(output.duration_seconds - source.duration_seconds) > duration_tolerance:
            raise AudioVerificationError("derivative duration changed unexpectedly")

        for key in _SELECTED_TAGS:
            expected = source.tags.get(key)
            if expected is not None and output.tags.get(key) != expected:
                raise AudioVerificationError(
                    f"derivative metadata tag {key} was not preserved"
                )

    @staticmethod
    def _required_text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AudioProbeError(f"ffprobe {field} is missing")
        return value.strip()

    @staticmethod
    def _positive_int(value: object, field: str) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise AudioProbeError(f"ffprobe {field} is invalid") from error
        if parsed <= 0:
            raise AudioProbeError(f"ffprobe {field} must be positive")
        return parsed

    @staticmethod
    def _positive_float(value: object, field: str) -> float:
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise AudioProbeError(f"ffprobe {field} is invalid") from error
        if parsed <= 0:
            raise AudioProbeError(f"ffprobe {field} must be positive")
        return parsed

    @staticmethod
    def _merge_tags(target: dict[str, str], raw: object) -> None:
        if not isinstance(raw, dict):
            return
        normalized = {str(key).casefold(): value for key, value in raw.items()}
        for key in _SELECTED_TAGS:
            value = normalized.get(key)
            if isinstance(value, str) and value.strip():
                target[key] = value.strip()
