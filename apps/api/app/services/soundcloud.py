import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from pydantic import ValidationError

from app.core.config import get_settings
from app.services.normalizer import RawSetPayload, normalize_raw_payload
from app.services.provider import (
    ProviderPayloadError,
    ProviderTemporaryError,
    ProviderValidationError,
)


_ALLOWED_HOSTS = {"soundcloud.com", "www.soundcloud.com"}
_BLOCKED_SEGMENTS = {"sets", "likes", "reposts"}
_REDIRECT_QUERY_KEYS = {
    "continue",
    "destination",
    "next",
    "redirect",
    "redirect_url",
    "return",
    "return_to",
    "url",
}
_PROCESS_TIMEOUT_SECONDS = 30
_MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class _OutputTooLarge(RuntimeError):
    pass


def validate_soundcloud_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ProviderValidationError(
            "soundcloud_invalid_url"
        ) from error
    if (
        parsed.scheme.casefold() != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise ProviderValidationError("soundcloud_invalid_url")

    path_parts = parsed.path.split("/")
    if path_parts and path_parts[-1] == "":
        path_parts.pop()
    if (
        len(path_parts) != 3
        or path_parts[0] != ""
        or not all(path_parts[1:])
    ):
        raise ProviderValidationError("soundcloud_invalid_url")
    decoded_parts = [unquote(part) for part in path_parts[1:]]
    if any(
        part in {"", ".", ".."}
        or "/" in part
        or "\\" in part
        or part.casefold() in _BLOCKED_SEGMENTS
        for part in decoded_parts
    ):
        raise ProviderValidationError("soundcloud_invalid_url")

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        candidate = value.strip()
        if key.casefold() in _REDIRECT_QUERY_KEYS and candidate:
            raise ProviderValidationError(
                "soundcloud_invalid_url"
            )
        try:
            candidate_scheme = urlsplit(
                candidate
            ).scheme.casefold()
        except ValueError as error:
            raise ProviderValidationError(
                "soundcloud_invalid_url"
            ) from error
        if candidate.startswith("//") or candidate_scheme in {
            "http",
            "https",
        }:
            raise ProviderValidationError(
                "soundcloud_invalid_url"
            )

    normalized_path = "/" + "/".join(path_parts[1:])
    return urlunsplit(
        ("https", host, normalized_path, parsed.query, "")
    )


async def _read_limited(stream: Any, output_limit_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > output_limit_bytes:
            raise _OutputTooLarge
        chunks.append(chunk)


async def _collect_output(
    process: Any,
    output_limit_bytes: int,
) -> tuple[bytes, bytes, int]:
    stdout_task = asyncio.create_task(
        _read_limited(process.stdout, output_limit_bytes)
    )
    stderr_task = asyncio.create_task(
        _read_limited(process.stderr, output_limit_bytes)
    )
    try:
        stdout, stderr = await asyncio.gather(
            stdout_task,
            stderr_task,
        )
    except BaseException:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(
            stdout_task,
            stderr_task,
            return_exceptions=True,
        )
        raise
    returncode = await process.wait()
    return stdout, stderr, returncode


async def _kill_and_wait(process: Any) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


class SoundCloudAdapter:
    def __init__(
        self,
        *,
        process_runner: Callable[..., Awaitable[Any]] | None = None,
        yt_dlp_bin: str | None = None,
        output_limit_bytes: int | None = None,
    ) -> None:
        settings = get_settings()
        self.process_runner = (
            asyncio.create_subprocess_exec
            if process_runner is None
            else process_runner
        )
        self.yt_dlp_bin = (
            settings.yt_dlp_bin
            if yt_dlp_bin is None
            else yt_dlp_bin
        )
        self.output_limit_bytes = (
            settings.provider_output_limit_bytes
            if output_limit_bytes is None
            else output_limit_bytes
        )
        if not 1 <= self.output_limit_bytes <= _MAX_OUTPUT_LIMIT_BYTES:
            raise ValueError("soundcloud_output_limit_invalid")
        self.process_timeout_seconds = _PROCESS_TIMEOUT_SECONDS

    async def fetch(self, url: str) -> RawSetPayload:
        validated_url = validate_soundcloud_url(url)
        argv = [
            self.yt_dlp_bin,
            "--ignore-config",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            validated_url,
        ]
        try:
            process = await self.process_runner(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ProviderTemporaryError(
                "soundcloud_process_error"
            ) from error

        try:
            async with asyncio.timeout(
                self.process_timeout_seconds
            ):
                stdout, _, returncode = await _collect_output(
                    process,
                    self.output_limit_bytes,
                )
        except TimeoutError as error:
            await _kill_and_wait(process)
            raise ProviderTemporaryError(
                "soundcloud_timeout"
            ) from error
        except _OutputTooLarge as error:
            await _kill_and_wait(process)
            raise ProviderPayloadError(
                "soundcloud_output_too_large"
            ) from error

        if returncode != 0:
            raise ProviderTemporaryError(
                "soundcloud_process_error"
            )
        try:
            raw = json.loads(stdout)
            if not isinstance(raw, dict):
                raise TypeError("expected one JSON object")
            normalized_input = {
                **raw,
                "webpage_url": validated_url,
            }
            return normalize_raw_payload(
                "soundcloud",
                normalized_input,
            )
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise ProviderPayloadError(
                "soundcloud_invalid_response"
            ) from error
