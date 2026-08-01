from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.schemas import (
    Candidate,
    CandidateCreate,
    ImportJob,
    ImportJobPage,
    ImportJobPatch,
    JobStatus,
    JobType,
    ReviewStatus,
    SearchProfile,
    SearchProfileCreate,
    SearchProfileUpdate,
    SetDetail,
    SetImage,
    SetPage,
    SetPatch,
    SetSource,
    SetSummary,
    UserRole,
)
from app.schemas.import_job import validate_job_transition
from app.repositories.base import ActiveProfileJobsError
from app.services.heuristic import HeuristicConfig, ScoreResult
from app.services.normalizer import RawSetPayload
from app.services.provider_sources import (
    SourceIntegrityError,
    legacy_source_to_provider_key,
    provider_key_to_legacy_source,
    sanitize_provider_metadata,
    validate_source_projection,
)


_SET_SELECT = """
select
    s.*,
    coalesce((
        select array_agg(a.name order by a.name)
        from set_artists sa join artists a on a.id = sa.artist_id
        where sa.set_id = s.id
    ), array[]::text[]) as artist_names,
    (
        select e.name from set_events se join events e on e.id = se.event_id
        where se.set_id = s.id order by e.created_at limit 1
    ) as event_name,
    (
        select e.venue from set_events se join events e on e.id = se.event_id
        where se.set_id = s.id order by e.created_at limit 1
    ) as venue,
    (
        select e.city from set_events se join events e on e.id = se.event_id
        where se.set_id = s.id order by e.created_at limit 1
    ) as city,
    (
        select extract(year from e.starts_on)::integer
        from set_events se join events e on e.id = se.event_id
        where se.set_id = s.id order by e.created_at limit 1
    ) as year,
    (
        select i.remote_url from set_images si join images i on i.id = si.image_id
        where si.set_id = s.id
        order by si.is_primary desc, si.priority desc, i.created_at
        limit 1
    ) as primary_image_url,
    (
        select p.key
        from set_provider_items spi
        join provider_items pi on pi.id = spi.provider_item_id
        join providers p on p.id = pi.provider_id
        where spi.set_id = s.id
          and spi.relationship = 'source'
          and spi.is_primary
        limit 1
    ) as linked_provider_key,
    (
        select pi.external_id
        from set_provider_items spi
        join provider_items pi on pi.id = spi.provider_item_id
        where spi.set_id = s.id
          and spi.relationship = 'source'
          and spi.is_primary
        limit 1
    ) as linked_provider_external_id,
    (
        select spi.is_primary
        from set_provider_items spi
        where spi.set_id = s.id
          and spi.relationship = 'source'
          and spi.is_primary
        limit 1
    ) as linked_is_primary
from sets s
"""

_PROFILE_SELECT = """
select
    sp.*,
    latest.id as latest_job_id,
    nullif(latest.details->>'last_result_count', '')::integer
        as last_result_count,
    latest.details->>'last_error_code' as last_error_code
from search_profiles sp
left join lateral (
    select id, details
    from import_jobs
    where search_profile_id = sp.id
    order by created_at desc, id desc
    limit 1
) latest on true
"""


def _job(row: dict[str, Any]) -> ImportJob:
    values = dict(row)
    values["url"] = values.pop("input_url", None)
    values["profile_id"] = values.pop("search_profile_id", None)
    values.pop("updated_at", None)
    return ImportJob(**values)


def _profile(row: dict[str, Any]) -> SearchProfile:
    return SearchProfile(**row)


def _candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(**row)


def _summary(row: dict[str, Any]) -> SetSummary:
    values = dict(row)
    validate_source_projection(
        legacy_source=values["source"],
        legacy_external_id=values["source_id"],
        provider_key=values.pop("linked_provider_key", None),
        provider_external_id=values.pop("linked_provider_external_id", None),
        is_primary=values.pop("linked_is_primary", None),
    )
    return SetSummary(
        **values,
        score_reasons=list(values.get("raw_payload", {}).get("score_reasons", [])),
        import_job_id=values.get("raw_payload", {}).get("import_job_id"),
        duplicate_of_id=values.get("raw_payload", {}).get("duplicate_of_id"),
    )


class PostgresRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def create_job(
        self,
        *,
        url: str | None,
        source: SetSource,
        job_type: JobType,
        profile_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> ImportJob:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    insert into import_jobs (
                        input_url, source, job_type, search_profile_id, details
                    ) values (%s, %s, %s, %s, %s)
                    returning *
                    """,
                    (
                        url,
                        source.value,
                        job_type.value,
                        profile_id,
                        Jsonb(details or {}),
                    ),
                ).fetchone()
        return _job(row)

    def get_job(self, job_id: UUID) -> ImportJob | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from import_jobs where id = %s", (job_id,)
                ).fetchone()
        return _job(row) if row else None

    def create_retry_job(self, job_id: UUID) -> tuple[ImportJob, bool] | None:
        """Serialize retries for a terminal job so duplicate deliveries share work."""
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                previous = cursor.execute(
                    "select * from import_jobs where id = %s for update",
                    (job_id,),
                ).fetchone()
                if (
                    previous is None
                    or previous["status"] not in {"failed", "dead_letter"}
                ):
                    return None
                active = cursor.execute(
                    """
                    select * from import_jobs
                    where details->>'retry_of_job_id' = %s
                      and status in ('queued', 'processing', 'retry')
                    order by created_at desc, id desc
                    limit 1
                    """,
                    (str(job_id),),
                ).fetchone()
                if active is not None:
                    return _job(active), False
                details = {
                    **dict(previous["details"] or {}),
                    "retry_of_job_id": str(job_id),
                }
                row = cursor.execute(
                    """
                    insert into import_jobs (
                        input_url, source, job_type, search_profile_id, details
                    ) values (%s, %s, %s, %s, %s)
                    returning *
                    """,
                    (
                        previous["input_url"],
                        previous["source"],
                        previous["job_type"],
                        previous["search_profile_id"],
                        Jsonb(details),
                    ),
                ).fetchone()
        return _job(row), True

    def claim_job(
        self,
        job_id: UUID,
        *,
        claim_ttl_seconds: int = 300,
    ) -> ImportJob | None:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    update import_jobs
                    set details = case
                            when status = 'processing' then
                                details || jsonb_build_object(
                                    'reclaim_count',
                                    coalesce(
                                        nullif(
                                            details->>'reclaim_count',
                                            ''
                                        )::integer,
                                        0
                                    ) + 1,
                                    'last_reclaimed_at',
                                    now(),
                                    'reclaimed_started_at',
                                    started_at
                                )
                            else details
                        end,
                        status = 'processing',
                        attempt_count = attempt_count + 1,
                        started_at = now(),
                        next_retry_at = null,
                        updated_at = now()
                    where id = %s
                      and (
                        status = 'queued'
                        or (
                            status = 'retry'
                            and (
                                next_retry_at is null
                                or next_retry_at <= now()
                            )
                        )
                        or (
                            status = 'processing'
                            and started_at is not null
                            and started_at
                                < now()
                                  - (%s * interval '1 second')
                        )
                      )
                    returning *
                    """,
                    (job_id, claim_ttl_seconds),
                ).fetchone()
        return _job(row) if row else None

    def list_recoverable_jobs(
        self,
        *,
        claim_ttl_seconds: int,
        limit: int,
    ) -> list[ImportJob]:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    """
                    select *
                    from import_jobs
                    where status = 'queued'
                       or (
                            status = 'retry'
                            and (
                                next_retry_at is null
                                or next_retry_at <= now()
                            )
                       )
                       or (
                            status = 'processing'
                            and started_at is not null
                            and started_at
                                < now()
                                  - (%s * interval '1 second')
                       )
                    order by created_at, id
                    limit %s
                    """,
                    (claim_ttl_seconds, limit),
                ).fetchall()
        return [_job(row) for row in rows]

    def list_jobs(
        self,
        *,
        source: SetSource | None,
        status: JobStatus | None,
        limit: int,
        offset: int,
    ) -> ImportJobPage:
        filters: list[str] = []
        params: list[Any] = []
        if source is not None:
            filters.append("source = %s")
            params.append(source.value)
        if status is not None:
            filters.append("status = %s")
            params.append(status.value)
        where = f" where {' and '.join(filters)}" if filters else ""
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                total = cursor.execute(
                    f"select count(*) as total from import_jobs{where}", params
                ).fetchone()["total"]
                rows = cursor.execute(
                    f"""
                    select * from import_jobs{where}
                    order by created_at desc, id desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                ).fetchall()
        return ImportJobPage(
            items=[_job(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def transition_job(
        self, job_id: UUID, patch: ImportJobPatch
    ) -> ImportJob | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    "select * from import_jobs where id = %s for update", (job_id,)
                ).fetchone()
                if current is None:
                    return None
                if patch.status is not None:
                    validate_job_transition(
                        JobStatus(current["status"]), patch.status
                    )
                changes = patch.model_dump(exclude_unset=True)
                if not changes:
                    return _job(current)
                assignments: list[str] = []
                params: list[Any] = []
                for field, value in changes.items():
                    assignments.append(f"{field} = %s")
                    params.append(
                        Jsonb(value)
                        if field == "details"
                        else value.value
                        if isinstance(value, (JobStatus, JobType, SetSource))
                        else value
                    )
                row = cursor.execute(
                    f"""
                    update import_jobs
                    set {", ".join(assignments)}, updated_at = now()
                    where id = %s
                    returning *
                    """,
                    [*params, job_id],
                ).fetchone()
        return _job(row)

    def transition_claimed_job(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        patch: ImportJobPatch,
    ) -> ImportJob | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select * from import_jobs
                    where id = %s
                      and status in ('processing', 'retry')
                      and started_at = %s
                    for update
                    """,
                    (job_id, claim_started_at),
                ).fetchone()
                if current is None:
                    return None
                if patch.status is not None:
                    validate_job_transition(
                        JobStatus(current["status"]), patch.status
                    )
                changes = patch.model_dump(exclude_unset=True)
                if not changes:
                    return _job(current)
                assignments: list[str] = []
                params: list[Any] = []
                for field, value in changes.items():
                    assignments.append(f"{field} = %s")
                    params.append(
                        Jsonb(value)
                        if field == "details"
                        else value.value
                        if isinstance(value, (JobStatus, JobType, SetSource))
                        else value
                    )
                row = cursor.execute(
                    f"""
                    update import_jobs
                    set {", ".join(assignments)}, updated_at = now()
                    where id = %s
                      and status in ('processing', 'retry')
                      and started_at = %s
                    returning *
                    """,
                    [*params, job_id, claim_started_at],
                ).fetchone()
        return _job(row) if row else None

    def complete_duplicate_job(
        self,
        job_id: UUID,
        duplicate_set_id: UUID,
        *,
        claim_started_at: datetime,
    ) -> ImportJob | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select * from import_jobs
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    for update
                    """,
                    (job_id, claim_started_at),
                ).fetchone()
                if current is None:
                    return None
                validate_job_transition(
                    JobStatus(current["status"]),
                    JobStatus.completed,
                )
                row = cursor.execute(
                    """
                    update import_jobs
                    set status = 'completed', finished_at = now(),
                        result_set_id = %s,
                        next_retry_at = null,
                        error_code = null, error_message = null,
                        details = details || %s,
                        updated_at = now()
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    returning *
                    """,
                    (
                        duplicate_set_id,
                        Jsonb(
                            {
                                "outcome": "duplicate",
                                "duplicate": True,
                            }
                        ),
                        job_id,
                        claim_started_at,
                    ),
                ).fetchone()
        return _job(row) if row else None

    def complete_discarded_job(
        self,
        job_id: UUID,
        score: ScoreResult,
        *,
        claim_started_at: datetime,
    ) -> ImportJob | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select * from import_jobs
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    for update
                    """,
                    (job_id, claim_started_at),
                ).fetchone()
                if current is None:
                    return None
                validate_job_transition(
                    JobStatus(current["status"]),
                    JobStatus.completed,
                )
                row = cursor.execute(
                    """
                    update import_jobs
                    set status = 'completed', finished_at = now(),
                        next_retry_at = null,
                        error_code = null, error_message = null,
                        details = details || %s,
                        updated_at = now()
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    returning *
                    """,
                    (
                        Jsonb(
                            {
                                "outcome": "discarded",
                                "score": score.score,
                                "score_reasons": score.reasons,
                            }
                        ),
                        job_id,
                        claim_started_at,
                    ),
                ).fetchone()
        return _job(row) if row else None

    def find_duplicate(
        self, payload: RawSetPayload, fingerprint: str
    ) -> UUID | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select id from sets
                    where (source = %s and source_id = %s)
                       or canonical_url = %s
                       or raw_payload->>'duplicate_fingerprint' = %s
                    order by
                        case
                            when source = %s and source_id = %s then 0
                            when canonical_url = %s then 1
                            else 2
                        end,
                        created_at
                    limit 1
                    """,
                    (
                        payload.source.value,
                        payload.source_id,
                        payload.canonical_url,
                        fingerprint,
                        payload.source.value,
                        payload.source_id,
                        payload.canonical_url,
                    ),
                ).fetchone()
        return row["id"] if row else None

    def persist_processed_set(
        self,
        *,
        payload: RawSetPayload,
        score: ScoreResult,
        candidates: list[CandidateCreate],
        job_id: UUID,
        fingerprint: str,
        claim_started_at: datetime,
    ) -> UUID | None:
        raw_payload = {
            **payload.raw_payload,
            "duplicate_fingerprint": fingerprint,
            "score_reasons": score.reasons,
            "import_job_id": str(job_id),
        }
        review_status = ReviewStatus.inbox
        provider_key = legacy_source_to_provider_key(payload.source)
        provider_metadata = sanitize_provider_metadata(payload.raw_payload)
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                admitted_identities = sorted(
                    (
                        f"fingerprint:{fingerprint}",
                        (
                            f"source:{payload.source.value}:"
                            f"{payload.source_id}"
                        ),
                        f"url:{payload.canonical_url}",
                    )
                )
                for identity in admitted_identities:
                    cursor.execute(
                        """
                        select pg_advisory_xact_lock(
                            hashtextextended(%s, 0)
                        )
                        """,
                        (identity,),
                    )
                job = cursor.execute(
                    """
                    select * from import_jobs
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    for update
                    """,
                    (job_id, claim_started_at),
                ).fetchone()
                if job is None:
                    return None
                validate_job_transition(
                    JobStatus(job["status"]), JobStatus.completed
                )
                duplicate = cursor.execute(
                    """
                    select id from sets
                    where (source = %s and source_id = %s)
                       or canonical_url = %s
                       or raw_payload->>'duplicate_fingerprint' = %s
                    order by
                        case
                            when source = %s and source_id = %s then 0
                            when canonical_url = %s then 1
                            else 2
                        end,
                        created_at
                    limit 1
                    """,
                    (
                        payload.source.value,
                        payload.source_id,
                        payload.canonical_url,
                        fingerprint,
                        payload.source.value,
                        payload.source_id,
                        payload.canonical_url,
                    ),
                ).fetchone()
                if duplicate is not None:
                    _assert_persisted_source_projection(cursor, duplicate["id"])
                    transitioned = cursor.execute(
                        """
                        update import_jobs
                        set status = 'completed', finished_at = now(),
                            result_set_id = %s,
                            next_retry_at = null,
                            error_code = null, error_message = null,
                            details = details || %s,
                            updated_at = now()
                        where id = %s and status = 'processing'
                          and started_at = %s
                        returning id
                        """,
                        (
                            duplicate["id"],
                            Jsonb(
                                {
                                    "outcome": "duplicate",
                                    "duplicate": True,
                                }
                            ),
                            job_id,
                            claim_started_at,
                        ),
                    ).fetchone()
                    if transitioned is None:
                        raise RuntimeError(
                            "Import job changed while persisting duplicate"
                        )
                    return duplicate["id"]
                provider = cursor.execute(
                    """
                    select id from providers where key = %s
                    """,
                    (provider_key,),
                ).fetchone()
                if provider is None:
                    raise SourceIntegrityError(
                        f"source projection provider {provider_key} is not registered"
                    )
                provider_item = cursor.execute(
                    """
                    insert into provider_items (
                        provider_id, external_id, canonical_url, title,
                        published_at, duration_seconds, raw_metadata,
                        metadata_fetched_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, now())
                    on conflict (provider_id, external_id) do update
                    set canonical_url = excluded.canonical_url,
                        title = excluded.title,
                        published_at = excluded.published_at,
                        duration_seconds = excluded.duration_seconds,
                        raw_metadata = excluded.raw_metadata,
                        metadata_fetched_at = excluded.metadata_fetched_at,
                        updated_at = now()
                    returning id
                    """,
                    (
                        provider["id"],
                        payload.source_id,
                        payload.canonical_url,
                        payload.title,
                        payload.published_at,
                        payload.duration_seconds,
                        Jsonb(provider_metadata),
                    ),
                ).fetchone()
                set_row = cursor.execute(
                    """
                    insert into sets (
                        source, source_id, canonical_url, title, description,
                        duration_seconds, published_at, set_score, review_status,
                        raw_payload
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (
                        payload.source.value,
                        payload.source_id,
                        payload.canonical_url,
                        payload.title,
                        payload.description,
                        payload.duration_seconds,
                        payload.published_at,
                        score.score,
                        review_status.value,
                        Jsonb(raw_payload),
                    ),
                ).fetchone()
                set_id = set_row["id"]
                cursor.execute(
                    """
                    insert into set_provider_items (
                        set_id, provider_item_id, relationship, is_primary
                    ) values (%s, %s, 'source', true)
                    """,
                    (set_id, provider_item["id"]),
                )
                _assert_persisted_source_projection(cursor, set_id)
                for candidate in candidates:
                    cursor.execute(
                        """
                        insert into field_candidates (
                            set_id, field_name, candidate_value, confidence, source
                        ) values (%s, %s, %s, %s, %s)
                        """,
                        (
                            set_id,
                            candidate.field_name,
                            candidate.candidate_value,
                            candidate.confidence,
                            candidate.source,
                        ),
                    )
                if payload.primary_image_url:
                    image = cursor.execute(
                        """
                        insert into images (remote_url, kind, attribution)
                        values (%s, 'thumbnail', %s)
                        returning id
                        """,
                        (
                            payload.primary_image_url,
                            f"{payload.source.value} provider thumbnail",
                        ),
                    ).fetchone()
                    cursor.execute(
                        """
                        insert into set_images (set_id, image_id, is_primary, priority)
                        values (%s, %s, true, 10)
                        """,
                        (set_id, image["id"]),
                    )
                transitioned = cursor.execute(
                    """
                    update import_jobs
                    set status = 'completed', finished_at = now(),
                        result_set_id = %s,
                        next_retry_at = null,
                        error_code = null, error_message = null,
                        details = details || %s,
                        updated_at = now()
                    where id = %s and status = 'processing'
                      and started_at = %s
                    returning id
                    """,
                    (
                        set_id,
                        Jsonb({"outcome": "persisted"}),
                        job_id,
                        claim_started_at,
                    ),
                ).fetchone()
                if transitioned is None:
                    raise RuntimeError(
                        "Import job changed while persisting processed set"
                    )
        return set_id

    def get_heuristic_config(self) -> HeuristicConfig:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from heuristic_config where id = 'active'"
                ).fetchone()
        if row is None:
            return HeuristicConfig()
        return HeuristicConfig(
            minimum_duration_seconds=row["minimum_duration_seconds"],
            review_threshold=float(row["review_threshold"]),
            auto_accept_threshold=float(row["high_confidence_threshold"]),
            strong_keywords=row["strong_keywords"],
            medium_keywords=row["medium_keywords"],
            genre_keywords=[],
            negative_keywords=row["negative_keywords"],
        )

    def get_user_role(self, user_id: UUID) -> UserRole | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select role from user_roles where user_id = %s", (user_id,)
                ).fetchone()
        return UserRole(row["role"]) if row else None

    def get_profile(self, profile_id: UUID) -> SearchProfile | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    (
                        f"{_PROFILE_SELECT} "
                        "where sp.id = %s and sp.deleted_at is null"
                    ),
                    (profile_id,),
                ).fetchone()
        return _profile(row) if row else None

    def checkpoint_profile_page(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        *,
        input_page_token: str | None,
        next_page_token: str | None,
        payloads: list[RawSetPayload],
        checkpoint_key: str = "youtube_page_checkpoint",
    ) -> ImportJob | None:
        checkpoint = {
            "input_page_token": input_page_token,
            "next_page_token": next_page_token,
            "source_ids": [payload.source_id for payload in payloads],
            "payloads": [
                payload.model_dump(mode="json")
                for payload in payloads
            ],
        }
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select * from import_jobs
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    for update
                    """,
                    (job_id, claim_started_at),
                ).fetchone()
                if current is None:
                    return None
                if checkpoint_key in current["details"]:
                    return _job(current)
                row = cursor.execute(
                    """
                    update import_jobs
                    set details = details || %s, updated_at = now()
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    returning *
                    """,
                    (
                        Jsonb(
                            {checkpoint_key: checkpoint}
                        ),
                        job_id,
                        claim_started_at,
                    ),
                ).fetchone()
        return _job(row) if row else None

    def get_or_create_child_job(
        self,
        parent_job_id: UUID,
        claim_started_at: datetime,
        payload: RawSetPayload,
    ) -> ImportJob | None:
        details = {
            "profile_job_id": str(parent_job_id),
            "source_id": payload.source_id,
        }
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                parent = cursor.execute(
                    """
                    select id from import_jobs
                    where id = %s
                      and job_type = 'search_profile'
                      and status = 'processing'
                      and started_at = %s
                    for update
                    """,
                    (parent_job_id, claim_started_at),
                ).fetchone()
                if parent is None:
                    return None
                row = cursor.execute(
                    """
                    insert into import_jobs (
                        input_url, source, job_type, details
                    ) values (%s, %s, 'url_import', %s)
                    on conflict (
                        (details->>'profile_job_id'),
                        (details->>'source_id')
                    )
                    where job_type = 'url_import'
                      and details ? 'profile_job_id'
                      and details ? 'source_id'
                    do nothing
                    returning *
                    """,
                    (
                        payload.canonical_url,
                        payload.source.value,
                        Jsonb(details),
                    ),
                ).fetchone()
                if row is None:
                    row = cursor.execute(
                        """
                        select * from import_jobs
                        where job_type = 'url_import'
                          and details->>'profile_job_id' = %s
                          and details->>'source_id' = %s
                        """,
                        (str(parent_job_id), payload.source_id),
                    ).fetchone()
        if row is None:
            raise RuntimeError("Profile child job could not be created")
        return _job(row)

    def finalize_profile_job(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        *,
        status: JobStatus,
        next_page_token: str | None,
        result_count: int,
        discard_count: int,
        duplicate_count: int,
        error_code: str | None,
        error_message: str | None,
    ) -> ImportJob | None:
        if status not in {
            JobStatus.completed,
            JobStatus.failed,
            JobStatus.blocked,
        }:
            raise ValueError("Profile final status must be completed, failed, or blocked")
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                job = cursor.execute(
                    """
                    select * from import_jobs
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    for update
                    """,
                    (job_id, claim_started_at),
                ).fetchone()
                if job is None:
                    return None
                profile_id = job["search_profile_id"]
                if profile_id is None:
                    raise ValueError(
                        f"Import job {job_id} has no search profile"
                    )
                validate_job_transition(
                    JobStatus(job["status"]),
                    status,
                )
                profile = cursor.execute(
                    """
                    select id from search_profiles
                    where id = %s for update
                    """,
                    (profile_id,),
                ).fetchone()
                latest_job = cursor.execute(
                    """
                    select id from import_jobs
                    where search_profile_id = %s
                    order by created_at desc, id desc
                    limit 1
                    """,
                    (profile_id,),
                ).fetchone()
                if (
                    profile is not None
                    and latest_job is not None
                    and latest_job["id"] == job_id
                ):
                    cursor.execute(
                        """
                        update search_profiles
                        set last_run_at = now(), next_page_token = %s
                        where id = %s
                        """,
                        (next_page_token, profile_id),
                    )
                row = cursor.execute(
                    """
                    update import_jobs
                    set status = %s, finished_at = now(),
                        error_code = %s, error_message = %s,
                        details = details || %s, updated_at = now()
                    where id = %s
                      and status = 'processing'
                      and started_at = %s
                    returning *
                    """,
                    (
                        status.value,
                        error_code,
                        error_message,
                        Jsonb(
                            {
                                "last_result_count": result_count,
                                "last_error_code": error_code,
                                "result_count": result_count,
                                "discard_count": discard_count,
                                "duplicate_count": duplicate_count,
                            }
                        ),
                        job_id,
                        claim_started_at,
                    ),
                ).fetchone()
        return _job(row) if row else None

    def list_sets(
        self,
        *,
        source: SetSource | None,
        status: ReviewStatus | None,
        min_score: float | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> SetPage:
        filters: list[str] = []
        params: list[Any] = []
        if source is not None:
            filters.append("s.source = %s")
            params.append(source.value)
        if status is not None:
            filters.append("s.review_status = %s")
            params.append(status.value)
        if min_score is not None:
            filters.append("s.set_score >= %s")
            params.append(min_score)
        if search:
            filters.append(
                """
                (
                    s.title ilike %s
                    or exists (
                        select 1 from set_artists sa join artists a on a.id = sa.artist_id
                        where sa.set_id = s.id and a.name ilike %s
                    )
                    or exists (
                        select 1 from set_events se join events e on e.id = se.event_id
                        where se.set_id = s.id and e.name ilike %s
                    )
                )
                """
            )
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        where = f" where {' and '.join(filters)}" if filters else ""
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                total = cursor.execute(
                    f"select count(*) as total from sets s{where}", params
                ).fetchone()["total"]
                rows = cursor.execute(
                    f"""
                    {_SET_SELECT}
                    {where}
                    order by s.created_at desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                ).fetchall()
        return SetPage(
            items=[_summary(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_set(self, set_id: UUID) -> SetDetail | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                return _fetch_set_detail(cursor, set_id)

    def update_set(
        self,
        set_id: UUID,
        patch: SetPatch,
        actor: str = "local-editor",
    ) -> SetDetail | None:
        changes = patch.model_dump(exclude_none=True)
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                exists = cursor.execute(
                    "select id from sets where id = %s for update", (set_id,)
                ).fetchone()
                if exists is None:
                    return None
                set_changes = {
                    key: value
                    for key, value in changes.items()
                    if key in {"title", "review_status"}
                }
                if set_changes:
                    assignments = ", ".join(
                        f"{field} = %s" for field in set_changes
                    )
                    values = [
                        value.value if isinstance(value, ReviewStatus) else value
                        for value in set_changes.values()
                    ]
                    cursor.execute(
                        f"update sets set {assignments}, updated_at = now() where id = %s",
                        [*values, set_id],
                    )
                event_changes = {
                    key: value
                    for key, value in changes.items()
                    if key in {"event_name", "venue", "city", "year"}
                }
                if event_changes:
                    event_id = _get_or_create_event(cursor, set_id)
                    _update_event(cursor, event_id, event_changes)
                cursor.execute(
                    """
                    insert into import_log (set_id, action, actor, details)
                    values (%s, 'updated', %s, %s)
                    """,
                    (set_id, actor, Jsonb(_jsonable(changes))),
                )
                return _fetch_set_detail(cursor, set_id)

    def decide_candidate(
        self, set_id: UUID, candidate_id: UUID, accepted: bool
    ) -> Candidate | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                set_row = cursor.execute(
                    """
                    select id, review_status from sets
                    where id = %s
                    for update
                    """,
                    (set_id,),
                ).fetchone()
                if set_row is None:
                    return None
                target = cursor.execute(
                    """
                    select * from field_candidates
                    where id = %s and set_id = %s
                    """,
                    (candidate_id, set_id),
                ).fetchone()
                if target is None:
                    return None
                affected_fields = _candidate_semantic_fields(
                    target["field_name"]
                )
                locked_candidates = cursor.execute(
                    """
                    select * from field_candidates
                    where set_id = %s
                      and field_name = any(%s)
                    order by id
                    for update
                    """,
                    (set_id, list(affected_fields)),
                ).fetchall()
                row = next(
                    (
                        item
                        for item in locked_candidates
                        if item["id"] == candidate_id
                    ),
                    None,
                )
                if row is None:
                    return None
                was_accepted = row["accepted"] is True
                updated = cursor.execute(
                    """
                    update field_candidates set accepted = %s
                    where id = %s returning *
                    """,
                    (accepted, candidate_id),
                ).fetchone()
                if accepted:
                    if row["field_name"] in {
                        "event",
                        "date",
                        "year",
                        "venue",
                        "city",
                    }:
                        cursor.execute(
                            """
                            update field_candidates
                            set accepted = false
                            where set_id = %s
                              and field_name = any(%s)
                              and id <> %s
                            """,
                            (
                                set_id,
                                list(affected_fields),
                                candidate_id,
                            ),
                        )
                    _apply_candidate(cursor, set_id, row)
                elif was_accepted:
                    _reverse_candidate(cursor, set_id, row)
                if (
                    set_row["review_status"]
                    == ReviewStatus.inbox.value
                ):
                    cursor.execute(
                        """
                        update sets set review_status = 'reviewing', updated_at = now()
                        where id = %s
                        """,
                        (set_id,),
                    )
                cursor.execute(
                    """
                    insert into import_log (set_id, action, details)
                    values (%s, %s, %s)
                    """,
                    (
                        set_id,
                        "candidate_accepted"
                        if accepted
                        else "candidate_rejected",
                        Jsonb({"candidate_id": str(candidate_id)}),
                    ),
                )
        return _candidate(updated)

    def list_profiles(self) -> list[SearchProfile]:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    (
                        f"{_PROFILE_SELECT} "
                        "where sp.deleted_at is null order by sp.name"
                    )
                ).fetchall()
        return [_profile(row) for row in rows]

    def create_profile(
        self, payload: SearchProfileCreate
    ) -> SearchProfile:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    insert into search_profiles (
                        name, query, source, operation, parameters,
                        schedule_cron, schedule_timezone, enabled
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    returning *
                    """,
                    (
                        payload.name,
                        payload.query,
                        payload.source,
                        payload.operation,
                        Jsonb(payload.parameters),
                        payload.schedule_cron,
                        payload.schedule_timezone,
                        payload.enabled,
                    ),
                ).fetchone()
        return _profile(row)

    def update_profile(
        self, profile_id: UUID, payload: SearchProfileUpdate
    ) -> SearchProfile | None:
        changes = payload.model_dump(exclude_none=True)
        if not changes:
            return self.get_profile(profile_id)
        schedule_changed = (
            "schedule_cron" in changes
            or "schedule_timezone" in changes
        )
        assignments = ", ".join(f"{field} = %s" for field in changes)
        if schedule_changed:
            assignments += ", next_scheduled_at = null"
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    f"""
                    update search_profiles set {assignments}
                    where id = %s and deleted_at is null returning *
                    """,
                    [*changes.values(), profile_id],
                ).fetchone()
        return _profile(row) if row else None

    def delete_profile(self, profile_id: UUID) -> bool:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                profile = cursor.execute(
                    """
                    select id from search_profiles
                    where id = %s and deleted_at is null
                    for update
                    """,
                    (profile_id,),
                ).fetchone()
                if profile is None:
                    return False
                active = cursor.execute(
                    """
                    select id from import_jobs
                    where search_profile_id = %s
                      and status in ('queued', 'processing', 'retry')
                    order by id
                    limit 1
                    for update
                    """,
                    (profile_id,),
                ).fetchone()
                if active is not None:
                    raise ActiveProfileJobsError(
                        "Search profile has an active import job"
                    )
                row = cursor.execute(
                    """
                    update search_profiles
                    set enabled = false, deleted_at = now()
                    where id = %s and deleted_at is null
                    returning id
                    """,
                    (profile_id,),
                ).fetchone()
        return row is not None

    def queue_profile(self, profile_id: UUID) -> ImportJob | None:
        result = self.queue_profile_with_creation(profile_id)
        return result[0] if result is not None else None

    def queue_profile_with_creation(
        self,
        profile_id: UUID,
    ) -> tuple[ImportJob, bool] | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                profile = cursor.execute(
                    """
                    select * from search_profiles
                    where id = %s and deleted_at is null
                    for update
                    """,
                    (profile_id,),
                ).fetchone()
                if profile is None:
                    return None
                active = cursor.execute(
                    """
                    select * from import_jobs
                    where search_profile_id = %s
                      and status in ('queued', 'processing', 'retry')
                    order by created_at desc, id desc
                    limit 1
                    """,
                    (profile_id,),
                ).fetchone()
                if active is not None:
                    return _job(active), False
                row = cursor.execute(
                    """
                    insert into import_jobs (
                        input_url, source, job_type, search_profile_id, details
                    ) values (%s, %s, 'search_profile', %s, %s)
                    returning *
                    """,
                    (
                        f"{profile['source']}-{profile['operation']}://{profile['query']}",
                        provider_key_to_legacy_source(profile["source"]).value,
                        profile_id,
                        Jsonb(
                            {
                                "provider_key": profile["source"],
                                "capability": "discovery",
                                "operation": profile["operation"],
                                "parameters": profile["parameters"],
                                "query": profile["query"],
                            }
                        ),
                    ),
                ).fetchone()
        return _job(row), True

    def mark_profile_scheduled(
        self,
        profile_id: UUID,
        *,
        scheduled_at: datetime,
        next_scheduled_at: datetime,
    ) -> SearchProfile | None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    update search_profiles
                    set last_scheduled_at = %s, next_scheduled_at = %s
                    where id = %s and deleted_at is null
                    returning *
                    """,
                    (scheduled_at, next_scheduled_at, profile_id),
                ).fetchone()
        return _profile(row) if row else None

    def stats(self) -> dict[str, Any]:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                total = cursor.execute(
                    "select count(*) as total from sets"
                ).fetchone()["total"]
                by_source_rows = cursor.execute(
                    "select source, count(*) as count from sets group by source"
                ).fetchall()
                by_status_rows = cursor.execute(
                    """
                    select review_status, count(*) as count
                    from sets group by review_status
                    """
                ).fetchall()
                score = cursor.execute(
                    """
                    select
                      count(*) filter (where set_score >= 0.7) as high,
                      count(*) filter (where set_score >= 0.4 and set_score < 0.7) as review,
                      count(*) filter (where set_score < 0.4) as low
                    from sets
                    """
                ).fetchone()
                queue_rows = cursor.execute(
                    """
                    select status, count(*) as count
                    from import_jobs
                    group by status
                    """
                ).fetchall()
        by_source = {source.value: 0 for source in SetSource}
        by_source.update(
            {row["source"]: row["count"] for row in by_source_rows}
        )
        by_status = {item.value: 0 for item in ReviewStatus}
        by_status.update(
            {row["review_status"]: row["count"] for row in by_status_rows}
        )
        job_counts = {status.value: 0 for status in JobStatus}
        job_counts.update({row["status"]: row["count"] for row in queue_rows})
        queue = {
            "queued": job_counts[JobStatus.queued.value],
            "processing": job_counts[JobStatus.processing.value],
            "failed": (
                job_counts[JobStatus.failed.value]
                + job_counts[JobStatus.dead_letter.value]
            ),
            "completed": job_counts[JobStatus.completed.value],
            "retry": job_counts[JobStatus.retry.value],
            "blocked": job_counts[JobStatus.blocked.value],
        }
        return {
            "total_sets": total,
            "by_source": by_source,
            "by_status": by_status,
            "score_bands": dict(score),
            "queue": queue,
        }

    def operational_metrics(
        self,
        *,
        claim_ttl_seconds: int,
    ) -> dict[str, int]:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select
                      count(*) filter (
                        where status = 'dead_letter'
                      ) as dead_letter_jobs,
                      count(*) filter (
                        where status = 'processing'
                          and started_at is not null
                          and started_at
                              < now() - (%s * interval '1 second')
                      ) as stuck_processing_jobs,
                      count(*) filter (
                        where error_code = 'youtube_quota_exceeded'
                      ) as provider_quota_failures,
                      count(*) filter (
                        where error_code = 'robots_denied'
                      ) as provider_robots_failures
                    from import_jobs
                    """,
                    (claim_ttl_seconds,),
                ).fetchone()
        if row is None:
            raise RuntimeError("Operational metrics query returned no row")
        return {
            "dead_letter_jobs": int(row["dead_letter_jobs"]),
            "stuck_processing_jobs": int(row["stuck_processing_jobs"]),
            "provider_quota_failures": int(row["provider_quota_failures"]),
            "provider_robots_failures": int(row["provider_robots_failures"]),
        }


def _assert_persisted_source_projection(cursor: Any, set_id: UUID) -> None:
    row = cursor.execute(
        """
        select
            sets.source,
            sets.source_id,
            providers.key as provider_key,
            provider_items.external_id as provider_external_id,
            links.is_primary
        from sets
        left join set_provider_items links
          on links.set_id = sets.id
         and links.relationship = 'source'
         and links.is_primary
        left join provider_items
          on provider_items.id = links.provider_item_id
        left join providers
          on providers.id = provider_items.provider_id
        where sets.id = %s
        """,
        (set_id,),
    ).fetchone()
    if row is None:
        raise SourceIntegrityError("source projection set does not exist")
    validate_source_projection(
        legacy_source=row["source"],
        legacy_external_id=row["source_id"],
        provider_key=row["provider_key"],
        provider_external_id=row["provider_external_id"],
        is_primary=row["is_primary"],
    )


def _get_or_create_event(cursor: Any, set_id: UUID) -> UUID:
    row = cursor.execute(
        """
        select e.id from set_events se join events e on e.id = se.event_id
        where se.set_id = %s order by e.created_at limit 1
        """,
        (set_id,),
    ).fetchone()
    if row:
        return row["id"]
    event = cursor.execute(
        "insert into events default values returning id"
    ).fetchone()
    cursor.execute(
        "insert into set_events (set_id, event_id) values (%s, %s)",
        (set_id, event["id"]),
    )
    return event["id"]


def _fetch_set_detail(cursor: Any, set_id: UUID) -> SetDetail | None:
    row = cursor.execute(
        f"{_SET_SELECT} where s.id = %s", (set_id,)
    ).fetchone()
    if row is None:
        return None
    candidates = cursor.execute(
        """
        select * from field_candidates
        where set_id = %s order by created_at
        """,
        (set_id,),
    ).fetchall()
    images = cursor.execute(
        """
        select i.*, si.is_primary, si.priority
        from set_images si join images i on i.id = si.image_id
        where si.set_id = %s
        order by si.is_primary desc, si.priority desc, i.created_at
        """,
        (set_id,),
    ).fetchall()
    return SetDetail(
        **_summary(row).model_dump(),
        description=row["description"],
        venue=row["venue"],
        year=row["year"],
        raw_payload=row["raw_payload"],
        candidates=[_candidate(item) for item in candidates],
        images=[SetImage(**item) for item in images],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _update_event(
    cursor: Any, event_id: UUID, changes: dict[str, Any]
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    field_map = {
        "event_name": "name",
        "venue": "venue",
        "city": "city",
    }
    for source, target in field_map.items():
        if source in changes:
            assignments.append(f"{target} = %s")
            values.append(changes[source])
    if "year" in changes:
        assignments.append(
            """
            starts_on = make_date(
                %s,
                coalesce(extract(month from starts_on)::integer, 1),
                coalesce(extract(day from starts_on)::integer, 1)
            )
            """
        )
        values.append(changes["year"])
    if assignments:
        cursor.execute(
            f"update events set {', '.join(assignments)} where id = %s",
            [*values, event_id],
        )


def _apply_candidate(
    cursor: Any, set_id: UUID, candidate: dict[str, Any]
) -> None:
    field = candidate["field_name"]
    value = candidate["candidate_value"]
    if field == "artist":
        artist = cursor.execute(
            """
            select id from artists
            where lower(name) = lower(%s)
            order by created_at
            limit 1
            """,
            (value,),
        ).fetchone()
        if artist is None:
            artist = cursor.execute(
                """
                insert into artists (name) values (%s)
                on conflict (name) do update set name = excluded.name
                returning id
                """,
                (value,),
            ).fetchone()
        cursor.execute(
            """
            insert into set_artists (set_id, artist_id)
            values (%s, %s) on conflict do nothing
            """,
            (set_id, artist["id"]),
        )
        return
    if field not in {"event", "venue", "city", "date", "year"}:
        return
    event_id = _get_or_create_event(cursor, set_id)
    if field == "date":
        cursor.execute(
            "update events set starts_on = %s where id = %s",
            (date.fromisoformat(value), event_id),
        )
    elif field == "year":
        cursor.execute(
            "update events set starts_on = %s where id = %s",
            (date(int(value), 1, 1), event_id),
        )
    else:
        _update_event(
            cursor,
            event_id,
            {
                {
                    "event": "event_name",
                    "venue": "venue",
                    "city": "city",
                }[field]: value
            },
        )


def _reverse_candidate(
    cursor: Any, set_id: UUID, candidate: dict[str, Any]
) -> None:
    field = candidate["field_name"]
    value = candidate["candidate_value"]
    if field == "artist":
        cursor.execute(
            """
            delete from set_artists sa
            using artists a
            where sa.set_id = %s
              and a.id = sa.artist_id
              and lower(a.name) = lower(%s)
              and not exists (
                  select 1
                  from field_candidates other
                  where other.set_id = %s
                    and other.id <> %s
                    and other.field_name = 'artist'
                    and other.accepted is true
                    and lower(other.candidate_value) = lower(%s)
              )
            """,
            (
                set_id,
                value,
                set_id,
                candidate["id"],
                value,
            ),
        )
        return
    if field not in {"event", "venue", "city", "date", "year"}:
        return
    if field in {"date", "year"}:
        remaining = cursor.execute(
            """
            select 1 from field_candidates
            where set_id = %s
              and field_name = any(%s)
              and accepted is true
            limit 1
            """,
            (set_id, ["date", "year"]),
        ).fetchone()
        if remaining is not None:
            return
    event = cursor.execute(
        """
        select e.*
        from set_events se join events e on e.id = se.event_id
        where se.set_id = %s
        order by e.created_at
        limit 1
        for update of e
        """,
        (set_id,),
    ).fetchone()
    if event is None:
        return
    cleared = False
    column_map = {
        "event": "name",
        "venue": "venue",
        "city": "city",
    }
    if field in column_map:
        column = column_map[field]
        current = event[column]
        if (
            current is not None
            and str(current).casefold() == str(value).casefold()
        ):
            cursor.execute(
                f"update events set {column} = null where id = %s",
                (event["id"],),
            )
            cleared = True
    elif field == "date":
        if event["starts_on"] == date.fromisoformat(value):
            cursor.execute(
                "update events set starts_on = null where id = %s",
                (event["id"],),
            )
            cleared = True
    elif field == "year":
        if event["starts_on"] == date(int(value), 1, 1):
            cursor.execute(
                "update events set starts_on = null where id = %s",
                (event["id"],),
            )
            cleared = True
    if cleared:
        cursor.execute(
            """
            delete from set_events se
            using events e
            where se.set_id = %s
              and se.event_id = %s
              and e.id = se.event_id
              and e.name is null
              and e.starts_on is null
              and e.venue is null
              and e.city is null
              and e.country is null
              and e.flyer_image_id is null
            """,
            (set_id, event["id"]),
        )


def _candidate_semantic_fields(field_name: str) -> tuple[str, ...]:
    if field_name in {"date", "year"}:
        return ("date", "year")
    return (field_name,)


def _jsonable(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.value if isinstance(value, ReviewStatus) else value
        for key, value in values.items()
    }
