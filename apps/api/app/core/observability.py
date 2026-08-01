from __future__ import annotations

import json
import logging
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from typing import Iterator
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)
_provider: ContextVar[str | None] = ContextVar("provider", default=None)
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)

_CONTEXT = {
    "request_id": _request_id,
    "job_id": _job_id,
    "provider": _provider,
    "task_id": _task_id,
}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_CREDENTIAL_URL = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"
)
_NAMED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)"
    r"(\s*[=:]\s*)([^\s,;&]+)"
)
_SECRET_FIELDS = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|database_url|dsn|password|"
    r"redis_url|secret|service_role|token)"
)
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_INTERNAL_EXTRA_FIELDS = {"message", "asctime"}


def _redact_text(value: str, secret_values: tuple[str, ...]) -> str:
    redacted = _BEARER.sub("Bearer [REDACTED]", value)
    redacted = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", redacted)
    redacted = _NAMED_SECRET.sub(r"\1\2[REDACTED]", redacted)
    for secret in secret_values:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact(value: object, secret_values: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return _redact_text(value, secret_values)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SECRET_FIELDS.search(str(key))
                else _redact(item, secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, secret_values) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), secret_values)


class StructuredJsonFormatter(logging.Formatter):
    """Emit one bounded, secret-redacted JSON object per log record."""

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.service = service
        self.environment = environment
        self.secret_values = tuple(
            sorted({value for value in secret_values if value}, key=len, reverse=True)
        )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.casefold(),
            "service": self.service,
            "environment": self.environment,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        for name, context in _CONTEXT.items():
            value = getattr(record, name, None) or context.get()
            if value is not None:
                payload[name] = value
        for name, value in record.__dict__.items():
            if (
                name not in _STANDARD_LOG_RECORD_FIELDS
                and name not in _INTERNAL_EXTRA_FIELDS
                and name not in payload
            ):
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            _redact(payload, self.secret_values),
            separators=(",", ":"),
            sort_keys=True,
        )


@contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    tokens = []
    try:
        for name, value in values.items():
            context = _CONTEXT.get(name)
            if context is not None:
                tokens.append((context, context.set(value)))
        yield
    finally:
        for context, token in reversed(tokens):
            context.reset(token)


def settings_secret_values(settings: object) -> tuple[str, ...]:
    names = (
        "database_url",
        "redis_url",
        "supabase_anon_key",
        "youtube_api_key",
    )
    return tuple(
        value
        for name in names
        if isinstance((value := getattr(settings, name, "")), str) and value
    )


def install_structured_logging(
    *,
    service: str,
    environment: str,
    secret_values: tuple[str, ...] = (),
    replace_handlers: bool = False,
) -> None:
    root = logging.getLogger()
    if replace_handlers:
        root.handlers.clear()
    existing = next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, "_syco23_structured", False)
        ),
        None,
    )
    formatter = StructuredJsonFormatter(
        service=service,
        environment=environment,
        secret_values=secret_values,
    )
    if existing is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._syco23_structured = True  # type: ignore[attr-defined]
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        existing.setFormatter(formatter)
    root.setLevel(logging.INFO)


class RequestCorrelationMiddleware:
    """Correlate safe request metadata without logging headers or bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = logging.getLogger("app.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else str(uuid4())
        started = perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
            await send(message)

        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                self.logger.info(
                    "HTTP request completed",
                    extra={
                        "event": "http_request_completed",
                        "request_id": request_id,
                        "method": scope.get("method", ""),
                        "path": scope.get("path", ""),
                        "status_code": status_code,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                    },
                )


def register_celery_observability(settings_getter) -> None:
    """Install process JSON logs and per-task correlation for workers and beat."""

    from celery.signals import setup_logging, task_postrun, task_prerun

    logger = logging.getLogger("app.tasks")
    task_contexts: dict[str, object] = {}

    def configure(**_kwargs: object) -> None:
        settings = settings_getter()
        install_structured_logging(
            service=settings.service_component,
            environment=settings.environment,
            secret_values=settings_secret_values(settings),
            replace_handlers=True,
        )

    def task_started(
        task_id: str,
        task: object,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        **_extra: object,
    ) -> None:
        job_id = (kwargs or {}).get("job_id")
        if job_id is None and args:
            candidate = str(args[0])
            if re.fullmatch(r"[0-9a-fA-F-]{36}", candidate):
                job_id = candidate
        request = getattr(task, "request", None)
        headers = getattr(request, "headers", None) or {}
        provider = headers.get("syco23_provider")
        token = _task_id.set(task_id)
        job_token = _job_id.set(str(job_id) if job_id is not None else None)
        provider_token = _provider.set(
            str(provider) if provider is not None else None
        )
        task_contexts[task_id] = (token, job_token, provider_token)
        logger.info(
            "Task started",
            extra={
                "event": "task_started",
                "task_id": task_id,
                "job_id": str(job_id) if job_id is not None else None,
                "provider": str(provider) if provider is not None else None,
                "task_name": getattr(task, "name", "unknown"),
            },
        )

    def task_finished(
        task_id: str,
        task: object,
        state: str | None = None,
        **_extra: object,
    ) -> None:
        logger.info(
            "Task finished",
            extra={
                "event": "task_finished",
                "task_id": task_id,
                "task_name": getattr(task, "name", "unknown"),
                "task_state": state or "unknown",
            },
        )
        tokens = task_contexts.pop(task_id, None)
        if isinstance(tokens, tuple) and len(tokens) == 3:
            _provider.reset(tokens[2])
            _job_id.reset(tokens[1])
            _task_id.reset(tokens[0])

    setup_logging.connect(configure, weak=False)
    task_prerun.connect(task_started, weak=False)
    task_postrun.connect(task_finished, weak=False)
