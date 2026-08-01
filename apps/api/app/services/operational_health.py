from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Protocol

from app.core.config import Settings
from app.repositories.base import Repository
from app.services.provider_health import descriptor_runtime_state
from app.services.provider_registry import ProviderRegistry


BEAT_HEARTBEAT_KEY = "syco23:observability:beat:last_success_at"
REDRIVE_FAILURE_KEY = "syco23:observability:redrive:publish_failures"
logger = logging.getLogger(__name__)


class _RedisClient(Protocol):
    def ping(self) -> object: ...

    def get(self, key: str) -> bytes | str | None: ...

    def set(self, key: str, value: object) -> object: ...


class _Inspector(Protocol):
    def active_queues(self) -> dict[str, list[dict[str, object]]] | None: ...


OperationalSnapshot = dict[str, object]
OperationalProbe = Callable[[ProviderRegistry], OperationalSnapshot]


def enabled_worker_queues(
    registry: ProviderRegistry,
    settings: Settings,
) -> set[str]:
    queues = {"process"}
    for descriptor in registry.descriptors():
        if not descriptor_runtime_state(descriptor, settings)["enabled"]:
            continue
        queues.update(workload.value for workload in descriptor.workload_by_capability.values())
    return queues


def _decoded(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _alert(
    code: str,
    *,
    value: int,
    threshold: int,
    severity: str,
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "active": value >= threshold,
        "value": value,
        "threshold": threshold,
    }


class OperationalHealthProbe:
    """Collect sanitized, aggregate production health without leaking failures."""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        *,
        redis_factory: Callable[[str], _RedisClient] | None = None,
        inspector_factory: Callable[[float], _Inspector] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.redis_factory = redis_factory or self._default_redis
        self.inspector_factory = inspector_factory or self._default_inspector
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic_clock = monotonic_clock or monotonic
        self._cache_lock = Lock()
        self._cached_until = 0.0
        self._cached_snapshot: OperationalSnapshot | None = None

    def _default_redis(self, url: str) -> _RedisClient:
        from redis import Redis

        return Redis.from_url(
            url,
            socket_connect_timeout=self.settings.health_probe_timeout_seconds,
            socket_timeout=self.settings.health_probe_timeout_seconds,
        )

    @staticmethod
    def _default_inspector(timeout: float) -> _Inspector:
        from app.workers.celery_app import celery_app

        return celery_app.control.inspect(timeout=timeout)

    def __call__(self, registry: ProviderRegistry) -> OperationalSnapshot:
        if self.settings.environment != "production":
            return {
                "ready": True,
                "dependencies": {
                    "postgres": {"ready": True, "status": "not_checked"},
                    "redis": {"ready": True, "status": "not_checked"},
                    "worker_queues": {
                        "ready": True,
                        "status": "not_checked",
                        "expected": sorted(enabled_worker_queues(registry, self.settings)),
                        "observed": [],
                        "missing": [],
                    },
                    "beat": {"ready": True, "status": "not_checked"},
                },
                "alerts": [],
            }

        with self._cache_lock:
            now = self.monotonic_clock()
            if self._cached_snapshot is not None and now < self._cached_until:
                return deepcopy(self._cached_snapshot)
            snapshot = self._collect(registry)
            self._cached_snapshot = snapshot
            self._cached_until = now + self.settings.health_probe_cache_seconds
            return deepcopy(snapshot)

    def _collect(self, registry: ProviderRegistry) -> OperationalSnapshot:

        metrics = {
            "dead_letter_jobs": 0,
            "stuck_processing_jobs": 0,
            "provider_quota_failures": 0,
            "provider_robots_failures": 0,
        }
        try:
            metrics.update(
                self.repository.operational_metrics(
                    claim_ttl_seconds=self.settings.job_claim_ttl_seconds
                )
            )
            postgres = {"ready": True, "status": "available"}
        except Exception:
            postgres = {"ready": False, "status": "unavailable"}

        redis_client: _RedisClient | None = None
        heartbeat: str | None = None
        redrive_failures = 0
        try:
            redis_client = self.redis_factory(self.settings.redis_url)
            if not redis_client.ping():
                raise RuntimeError("Redis ping failed")
            heartbeat = _decoded(redis_client.get(BEAT_HEARTBEAT_KEY))
            failure_value = _decoded(redis_client.get(REDRIVE_FAILURE_KEY))
            redrive_failures = int(failure_value or "0")
            redis_state = {"ready": True, "status": "available"}
        except Exception:
            redis_state = {"ready": False, "status": "unavailable"}

        expected = enabled_worker_queues(registry, self.settings)
        observed: set[str] = set()
        worker_status = "available"
        try:
            inspector = self.inspector_factory(self.settings.health_probe_timeout_seconds)
            responses = inspector.active_queues()
            if not responses:
                raise RuntimeError("No worker response")
            for queues in responses.values():
                observed.update(
                    str(queue["name"])
                    for queue in queues
                    if isinstance(queue, dict) and queue.get("name")
                )
        except Exception:
            worker_status = "unavailable"
        missing = expected - observed
        workers_ready = worker_status == "available" and not missing
        worker_queues = {
            "ready": workers_ready,
            "status": worker_status,
            "expected": sorted(expected),
            "observed": sorted(observed),
            "missing": sorted(missing),
        }

        beat = self._beat_state(heartbeat)
        dependencies = {
            "postgres": postgres,
            "redis": redis_state,
            "worker_queues": worker_queues,
            "beat": beat,
        }
        alerts = sorted(
            (
                _alert(
                    "dead_letter_growth",
                    value=metrics["dead_letter_jobs"],
                    threshold=self.settings.dead_letter_alert_threshold,
                    severity="critical",
                ),
                _alert(
                    "stuck_processing_jobs",
                    value=metrics["stuck_processing_jobs"],
                    threshold=self.settings.stuck_job_alert_threshold,
                    severity="critical",
                ),
                _alert(
                    "redrive_publish_failures",
                    value=redrive_failures,
                    threshold=self.settings.redrive_failure_alert_threshold,
                    severity="critical",
                ),
                _alert(
                    "provider_quota_failures",
                    value=metrics["provider_quota_failures"],
                    threshold=self.settings.provider_failure_alert_threshold,
                    severity="warning",
                ),
                _alert(
                    "provider_robots_failures",
                    value=metrics["provider_robots_failures"],
                    threshold=self.settings.provider_failure_alert_threshold,
                    severity="warning",
                ),
            ),
            key=lambda item: str(item["code"]),
        )
        return {
            "ready": all(bool(item["ready"]) for item in dependencies.values()),
            "dependencies": dependencies,
            "alerts": alerts,
        }

    def _beat_state(self, heartbeat: str | None) -> dict[str, object]:
        if heartbeat is None:
            return {
                "ready": False,
                "status": "missing",
                "stale_after_seconds": self.settings.beat_stale_after_seconds,
            }
        try:
            recorded_at = datetime.fromisoformat(heartbeat)
            if recorded_at.tzinfo is None:
                raise ValueError("Heartbeat is not timezone-aware")
            age = max(0, int((self.clock() - recorded_at).total_seconds()))
        except (TypeError, ValueError):
            return {
                "ready": False,
                "status": "invalid",
                "stale_after_seconds": self.settings.beat_stale_after_seconds,
            }
        ready = age <= self.settings.beat_stale_after_seconds
        return {
            "ready": ready,
            "status": "available" if ready else "stale",
            "age_seconds": age,
            "stale_after_seconds": self.settings.beat_stale_after_seconds,
        }


def record_periodic_task_success(
    settings: Settings,
    *,
    task_name: str,
    redrive_publish_failures: int | None = None,
    redis_factory: Callable[[str], _RedisClient] | None = None,
) -> None:
    """Record successful beat-to-worker delivery without changing task outcomes."""

    if settings.environment != "production":
        return
    try:
        if redis_factory is None:
            from redis import Redis

            client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.health_probe_timeout_seconds,
                socket_timeout=settings.health_probe_timeout_seconds,
            )
        else:
            client = redis_factory(settings.redis_url)
        recorded_at = datetime.now(UTC).isoformat()
        client.set(BEAT_HEARTBEAT_KEY, recorded_at)
        if redrive_publish_failures is not None:
            client.set(REDRIVE_FAILURE_KEY, redrive_publish_failures)
        logger.info(
            "Periodic task heartbeat recorded",
            extra={
                "event": "periodic_task_heartbeat",
                "task_name": task_name,
                "redrive_publish_failures": redrive_publish_failures,
            },
        )
    except Exception as error:
        logger.warning(
            "Periodic task heartbeat failed",
            extra={
                "event": "periodic_task_heartbeat_failed",
                "task_name": task_name,
                "error_type": type(error).__name__,
            },
        )
