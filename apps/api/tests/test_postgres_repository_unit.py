from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

from app.repositories.postgres import PostgresRepository
from app.schemas import JobStatus, JobType, SetSource
from app.services.heuristic import ScoreResult
from app.services.normalizer import RawSetPayload


class FakeCursor(AbstractContextManager["FakeCursor"]):
    def __init__(
        self,
        *,
        job_status: JobStatus = JobStatus.processing,
        duplicate_id: UUID | None = None,
    ) -> None:
        self.job_status = job_status
        self.duplicate_id = duplicate_id
        self.executed: list[tuple[str, object]] = []
        self._row: dict[str, Any] | None = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> "FakeCursor":
        normalized = " ".join(statement.split()).casefold()
        self.executed.append((normalized, params))
        if "select * from import_jobs" in normalized and "for update" in normalized:
            self._row = (
                None
                if self.job_status is not JobStatus.processing
                and "status = 'processing'" in normalized
                else {"status": self.job_status.value}
            )
        elif "select id from sets" in normalized:
            self._row = (
                {"id": self.duplicate_id}
                if self.duplicate_id is not None
                else None
            )
        elif "insert into sets" in normalized:
            self._row = {"id": UUID("00000000-0000-4000-8000-000000030001")}
        elif "update import_jobs" in normalized:
            self._row = {"id": UUID("00000000-0000-4000-8000-000000030002")}
        else:
            self._row = None
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class FakeConnection(AbstractContextManager["FakeConnection"]):
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.rolled_back = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.rolled_back = exc_type is not None
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self.connection_instance = FakeConnection(cursor)

    def connection(self) -> FakeConnection:
        return self.connection_instance


def _payload() -> RawSetPayload:
    return RawSetPayload(
        source=SetSource.soundcloud,
        source_id="atomic-persist",
        canonical_url="https://soundcloud.com/syco23/atomic-persist",
        title="Atomic Persist",
        duration_seconds=3600,
        published_at=datetime.now(UTC),
        raw_payload={},
    )


def _persist(repository: PostgresRepository) -> UUID:
    result = repository.persist_processed_set(
        payload=_payload(),
        score=ScoreResult(
            score=0.8,
            accepted=True,
            auto_accept=True,
            reasons=["test"],
        ),
        candidates=[],
        job_id=UUID("00000000-0000-4000-8000-000000030002"),
        fingerprint="atomic-fingerprint",
        claim_started_at=datetime.now(UTC),
    )
    assert result is not None
    return result


def test_persist_processed_set_locks_fingerprint_and_processing_job() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    repository = PostgresRepository(pool)  # type: ignore[arg-type]

    _persist(repository)

    statements = [statement for statement, _ in cursor.executed]
    advisory_calls = [
        (index, params)
        for index, (statement, params) in enumerate(cursor.executed)
        if "pg_advisory_xact_lock" in statement
    ]
    expected_identities = sorted(
        [
            "fingerprint:atomic-fingerprint",
            "source:soundcloud:atomic-persist",
                "url:soundcloud:https://soundcloud.com/syco23/atomic-persist",
        ]
    )
    assert [params[0] for _, params in advisory_calls] == expected_identities
    advisory_index = advisory_calls[-1][0]
    job_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if "select * from import_jobs" in statement and "for update" in statement
    )
    duplicate_index = next(
        index
        for index, statement in enumerate(statements)
        if "select id from sets" in statement
    )
    insert_index = next(
        index
        for index, statement in enumerate(statements)
        if "insert into sets" in statement
    )
    assert advisory_index < job_lock_index < duplicate_index < insert_index
    job_update = next(
        statement
        for statement in statements
        if "update import_jobs" in statement
    )
    assert "status = 'processing'" in job_update


class DecisionCursor(AbstractContextManager["DecisionCursor"]):
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self._row: dict[str, Any] | None = None
        self._rows: list[dict[str, Any]] = []
        self.candidate = {
            "id": UUID("00000000-0000-4000-8000-000000030020"),
            "set_id": UUID("00000000-0000-4000-8000-000000030021"),
            "field_name": "custom",
            "candidate_value": "value",
            "confidence": 0.8,
            "source": "test",
            "accepted": None,
            "created_at": datetime.now(UTC),
        }

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> "DecisionCursor":
        normalized = " ".join(statement.split()).casefold()
        self.executed.append((normalized, params))
        self._rows = []
        if "select id, review_status from sets" in normalized:
            self._row = {
                "id": self.candidate["set_id"],
                "review_status": "inbox",
            }
        elif (
            "select * from field_candidates" in normalized
            and "order by id" not in normalized
        ):
            self._row = dict(self.candidate)
        elif (
            "select * from field_candidates" in normalized
            and "order by id" in normalized
            and "for update" in normalized
        ):
            self._row = None
            self._rows = [dict(self.candidate)]
        elif "update field_candidates set accepted" in normalized:
            self._row = {**self.candidate, "accepted": True}
        else:
            self._row = None
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


def test_candidate_decision_locks_set_before_ordered_candidate_group() -> None:
    cursor = DecisionCursor()
    repository = PostgresRepository(FakePool(cursor))  # type: ignore[arg-type]

    decided = repository.decide_candidate(
        cursor.candidate["set_id"],
        cursor.candidate["id"],
        True,
    )

    assert decided is not None and decided.accepted is True
    statements = [statement for statement, _ in cursor.executed]
    set_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if "select id, review_status from sets" in statement
        and "for update" in statement
    )
    candidate_group_index = next(
        index
        for index, statement in enumerate(statements)
        if "select * from field_candidates" in statement
        and "order by id" in statement
        and "for update" in statement
    )
    decision_index = next(
        index
        for index, statement in enumerate(statements)
        if "update field_candidates set accepted" in statement
    )
    assert set_lock_index < candidate_group_index < decision_index


def test_persist_processed_set_rejects_terminal_job_without_writes() -> None:
    cursor = FakeCursor(job_status=JobStatus.completed)
    pool = FakePool(cursor)
    repository = PostgresRepository(pool)  # type: ignore[arg-type]

    result = repository.persist_processed_set(
        payload=_payload(),
        score=ScoreResult(
            score=0.8,
            accepted=True,
            auto_accept=True,
            reasons=["test"],
        ),
        candidates=[],
        job_id=UUID("00000000-0000-4000-8000-000000030002"),
        fingerprint="atomic-fingerprint",
        claim_started_at=datetime.now(UTC),
    )

    assert result is None
    assert not any(
        "insert into sets" in statement for statement, _ in cursor.executed
    )


def _job_row(
    job_id: UUID,
    *,
    status: JobStatus,
    profile_id: UUID | None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "input_url": "youtube-search://postgres unit",
        "source": SetSource.youtube.value,
        "job_type": JobType.search_profile.value,
        "search_profile_id": profile_id,
        "status": status.value,
        "attempt_count": 1,
        "created_at": datetime.now(UTC),
        "started_at": datetime.now(UTC),
        "finished_at": None,
        "next_retry_at": None,
        "result_set_id": None,
        "error_code": None,
        "error_message": None,
        "details": details or {"query": "postgres unit"},
    }


class ProfileOperationsCursor(AbstractContextManager["ProfileOperationsCursor"]):
    def __init__(self) -> None:
        self.profile_id = UUID(
            "00000000-0000-4000-8000-000000030100"
        )
        self.older_id = UUID(
            "00000000-0000-4000-8000-000000030101"
        )
        self.newer_id = UUID(
            "00000000-0000-4000-8000-000000030102"
        )
        self.child_id = UUID(
            "00000000-0000-4000-8000-000000030103"
        )
        self.executed: list[tuple[str, object]] = []
        self._row: dict[str, Any] | None = None
        self.profile_updated = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(
        self,
        statement: str,
        params: object = None,
    ) -> "ProfileOperationsCursor":
        normalized = " ".join(statement.split()).casefold()
        self.executed.append((normalized, params))
        if (
            "select * from import_jobs" in normalized
            and "where id = %s" in normalized
            and "for update" in normalized
        ):
            self._row = _job_row(
                self.older_id,
                status=JobStatus.processing,
                profile_id=self.profile_id,
            )
        elif "select id from search_profiles" in normalized:
            self._row = {"id": self.profile_id}
        elif "select * from search_profiles" in normalized:
            self._row = {
                "id": self.profile_id,
                "query": "postgres unit",
            }
        elif (
            "select * from import_jobs" in normalized
            and "status in ('queued', 'processing', 'retry')" in normalized
        ):
            self._row = _job_row(
                self.newer_id,
                status=JobStatus.queued,
                profile_id=self.profile_id,
            )
        elif (
            "select id from import_jobs" in normalized
            and "search_profile_id" in normalized
        ):
            self._row = {"id": self.newer_id}
        elif "update search_profiles" in normalized:
            self.profile_updated = True
            self._row = None
        elif (
            "update import_jobs" in normalized
            and "finished_at = now()" in normalized
        ):
            values = params
            self._row = _job_row(
                self.older_id,
                status=JobStatus.completed,
                profile_id=self.profile_id,
                details={
                    "query": "postgres unit",
                    "result_count": 1,
                    "discard_count": 2,
                    "duplicate_count": 3,
                },
            )
        elif (
            "select id from import_jobs" in normalized
            and "job_type = 'search_profile'" in normalized
        ):
            self._row = {"id": self.older_id}
        elif "insert into import_jobs" in normalized:
            self._row = None
        elif (
            "select * from import_jobs" in normalized
            and "details->>'profile_job_id'" in normalized
        ):
            self._row = {
                **_job_row(
                    self.child_id,
                    status=JobStatus.completed,
                    profile_id=None,
                    details={
                        "profile_job_id": str(self.older_id),
                        "source_id": "postgres-child",
                        "outcome": "persisted",
                    },
                ),
                "input_url": (
                    "https://www.youtube.com/watch?v=postgres-child"
                ),
                "job_type": JobType.url_import.value,
            }
        else:
            self._row = None
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


def test_postgres_profile_finalize_targets_parent_without_stale_cursor() -> None:
    cursor = ProfileOperationsCursor()
    repository = PostgresRepository(FakePool(cursor))  # type: ignore[arg-type]

    finalized = repository.finalize_profile_job(
        cursor.older_id,
        datetime.now(UTC),
        status=JobStatus.completed,
        next_page_token="OLDER",
        result_count=1,
        discard_count=2,
        duplicate_count=3,
        error_code=None,
        error_message=None,
    )

    assert finalized.id == cursor.older_id
    assert finalized.status is JobStatus.completed
    assert finalized.details["duplicate_count"] == 3
    assert cursor.profile_updated is False
    final_update = next(
        params
        for statement, params in cursor.executed
        if "update import_jobs" in statement
        and "finished_at = now()" in statement
    )
    assert final_update[-2] == cursor.older_id


def test_postgres_profile_child_conflict_reuses_existing_job() -> None:
    cursor = ProfileOperationsCursor()
    repository = PostgresRepository(FakePool(cursor))  # type: ignore[arg-type]
    payload = RawSetPayload(
        source=SetSource.youtube,
        source_id="postgres-child",
        canonical_url=(
            "https://www.youtube.com/watch?v=postgres-child"
        ),
        title="Postgres child liveset",
        duration_seconds=3_600,
        raw_payload={"id": "postgres-child"},
    )

    child = repository.get_or_create_child_job(
        cursor.older_id,
        datetime.now(UTC),
        payload,
    )

    assert child.id == cursor.child_id
    assert child.details["profile_job_id"] == str(cursor.older_id)
    assert child.details["source_id"] == "postgres-child"


def test_postgres_profile_queue_reuses_active_job() -> None:
    cursor = ProfileOperationsCursor()
    repository = PostgresRepository(FakePool(cursor))  # type: ignore[arg-type]

    queued = repository.queue_profile(cursor.profile_id)

    assert queued is not None
    assert queued.id == cursor.newer_id
    assert not any(
        "insert into import_jobs" in statement
        for statement, _ in cursor.executed
    )
    statements = [statement for statement, _ in cursor.executed]
    profile_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if "select * from search_profiles" in statement
        and "for update" in statement
    )
    active_job_index = next(
        index
        for index, statement in enumerate(statements)
        if "select * from import_jobs" in statement
        and "status in ('queued', 'processing', 'retry')" in statement
    )
    assert profile_lock_index < active_job_index


def test_persist_processed_set_rechecks_duplicate_inside_lock() -> None:
    duplicate_id = UUID("00000000-0000-4000-8000-000000030099")
    cursor = FakeCursor(duplicate_id=duplicate_id)
    repository = PostgresRepository(FakePool(cursor))  # type: ignore[arg-type]

    result = _persist(repository)

    assert result == duplicate_id
    assert not any(
        "insert into sets" in statement for statement, _ in cursor.executed
    )


class RetryCursor(AbstractContextManager["RetryCursor"]):
    def __init__(self) -> None:
        self.parent_id = UUID("00000000-0000-4000-8000-000000030201")
        self.active_id = UUID("00000000-0000-4000-8000-000000030202")
        self.executed: list[tuple[str, object]] = []
        self._row: dict[str, Any] | None = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> "RetryCursor":
        normalized = " ".join(statement.split()).casefold()
        self.executed.append((normalized, params))
        if "details->>'retry_of_job_id'" in normalized:
            self._row = _job_row(
                self.active_id,
                status=JobStatus.queued,
                profile_id=None,
                details={"retry_of_job_id": str(self.parent_id)},
            )
        elif "select * from import_jobs where id = %s for update" in normalized:
            self._row = _job_row(
                self.parent_id,
                status=JobStatus.failed,
                profile_id=None,
            )
        else:
            self._row = None
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


def test_postgres_retry_locks_terminal_parent_before_reusing_active_child() -> None:
    """Without the parent row lock, simultaneous admin retries could both insert."""
    cursor = RetryCursor()
    repository = PostgresRepository(FakePool(cursor))  # type: ignore[arg-type]

    result = repository.create_retry_job(cursor.parent_id)

    assert result is not None
    job, created = result
    assert job.id == cursor.active_id
    assert created is False
    statements = [statement for statement, _ in cursor.executed]
    parent_lock = next(
        index
        for index, statement in enumerate(statements)
        if "select * from import_jobs where id = %s for update" in statement
    )
    active_lookup = next(
        index
        for index, statement in enumerate(statements)
        if "details->>'retry_of_job_id'" in statement
    )
    assert parent_lock < active_lookup
    assert not any("insert into import_jobs" in statement for statement in statements)
