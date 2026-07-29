from datetime import datetime
from uuid import UUID

from app.services.normalizer import RawSetPayload
from app.workers.celery_app import celery_app


_PROCESS_TASK = "app.workers.normalize_worker.process_raw_payload"


def dispatch_process_payload(
    job_id: UUID,
    payload: RawSetPayload,
    claim_started_at: datetime,
) -> None:
    celery_app.signature(
        _PROCESS_TASK,
        args=(
            str(job_id),
            payload.model_dump(mode="json"),
            claim_started_at.isoformat(),
        ),
    ).apply_async()
