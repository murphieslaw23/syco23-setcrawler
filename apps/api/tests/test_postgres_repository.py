import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID

import pytest

from app.schemas import (
    ImportJobPatch,
    JobStatus,
    JobType,
    ReviewStatus,
    SearchProfileCreate,
    SetPatch,
    SetSource,
)
from app.services.heuristic import ScoreResult
from app.services.normalizer import RawSetPayload
from app.repositories.base import ActiveProfileJobsError


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "TEST_DATABASE_URL is not set; "
        "PostgreSQL repository integration test skipped"
    ),
)
def test_postgres_repository_jobs_profiles_sets_and_curated_writes() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(TEST_DATABASE_URL)
    pool.open()
    repository = postgres.PostgresRepository(pool)
    set_id = UUID("00000000-0000-4000-8000-000000020001")
    candidate_id = UUID("00000000-0000-4000-8000-000000020002")
    competing_candidate_id = UUID(
        "00000000-0000-4000-8000-000000020004"
    )
    user_id = UUID("00000000-0000-4000-8000-000000020003")
    now = datetime.now(UTC)
    event_id = None
    profile_id = None
    profile_job_id = None
    race_job_ids: list[UUID] = []
    race_set_id = None

    try:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into sets (
                        id, source, source_id, canonical_url, title, description,
                        duration_seconds, published_at, set_score, review_status, raw_payload
                    ) values (%s, 'soundcloud', 'task-2', %s, 'Task 2 Set', 'Description',
                              3600, %s, 0.75, 'inbox', '{}'::jsonb)
                    on conflict (id) do update set review_status = 'inbox'
                    """,
                    (set_id, "https://soundcloud.com/syco23/task-2", now),
                )
                cursor.execute(
                    """
                    insert into field_candidates (
                        id, set_id, field_name, candidate_value, confidence, source
                    ) values (%s, %s, 'city', 'Berlin', 0.9, 'title')
                    on conflict (id) do update set accepted = null
                    """,
                    (candidate_id, set_id),
                )
                cursor.execute(
                    """
                    insert into field_candidates (
                        id, set_id, field_name, candidate_value, confidence, source
                    ) values (%s, %s, 'city', 'Prague', 0.95, 'description')
                    on conflict (id) do update set accepted = null
                    """,
                    (competing_candidate_id, set_id),
                )
                cursor.execute(
                    "insert into auth.users (id) values (%s) on conflict do nothing",
                    (user_id,),
                )
                cursor.execute(
                    """
                    insert into user_roles (user_id, role) values (%s, 'editor')
                    on conflict (user_id) do update set role = excluded.role
                    """,
                    (user_id,),
                )

        job = repository.create_job(
            url="https://soundcloud.com/syco23/task-2",
            source=SetSource.soundcloud,
            job_type=JobType.url_import,
        )
        initial_claim = repository.claim_job(job.id)
        active_competing_claim = repository.claim_job(job.id)
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update import_jobs
                    set started_at = now() - interval '301 seconds',
                        next_retry_at = now() + interval '1 hour'
                    where id = %s
                    """,
                    (job.id,),
                )
        transitioned = repository.claim_job(job.id)
        page = repository.list_jobs(
            source=SetSource.soundcloud,
            status=JobStatus.processing,
            limit=10,
            offset=0,
        )
        decided = repository.decide_candidate(set_id, candidate_id, True)
        detail = repository.get_set(set_id)

        assert initial_claim is not None
        assert active_competing_claim is None
        assert transitioned is not None
        assert transitioned.status is JobStatus.processing
        assert transitioned.attempt_count == 2
        assert transitioned.next_retry_at is None
        assert transitioned.details["reclaim_count"] == 1
        assert transitioned.details["last_reclaimed_at"]
        assert transitioned.details["reclaimed_started_at"]
        assert page.total >= 1
        assert decided is not None and decided.accepted is True
        assert detail is not None
        assert detail.city == "Berlin"
        assert detail.review_status is ReviewStatus.reviewing
        competing = repository.decide_candidate(
            set_id, competing_candidate_id, True
        )
        competed_detail = repository.get_set(set_id)
        assert competing is not None and competing.accepted is True
        assert competed_detail is not None
        assert competed_detail.city == "Prague"
        assert next(
            item
            for item in competed_detail.candidates
            if item.id == candidate_id
        ).accepted is False
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                event_id = cursor.execute(
                    """
                    select event_id from set_events
                    where set_id = %s limit 1
                    """,
                    (set_id,),
                ).fetchone()["event_id"]
        repository.decide_candidate(
            set_id, competing_candidate_id, False
        )
        assert repository.get_set(set_id).city is None

        profile = repository.create_profile(
            SearchProfileCreate(
                name="Task 2 durable outcome",
                query="task 2 durable outcome",
            )
        )
        profile_id = profile.id
        profile_job = repository.queue_profile(profile.id)
        assert profile_job is not None
        profile_job_id = profile_job.id
        with pytest.raises(ActiveProfileJobsError):
            repository.delete_profile(profile.id)
        claimed_profile_job = repository.claim_job(profile_job.id)
        assert claimed_profile_job is not None
        assert claimed_profile_job.started_at is not None
        repository.finalize_profile_job(
            profile_job.id,
            claimed_profile_job.started_at,
            status=JobStatus.failed,
            next_page_token="next",
            result_count=23,
            discard_count=4,
            duplicate_count=5,
            error_code="quota_limited",
            error_message="quota_limited",
        )
        fetched_profile = repository.get_profile(profile.id)
        listed_profile = next(
            item
            for item in repository.list_profiles()
            if item.id == profile.id
        )
        assert repository.get_job(profile_job.id).id == profile_job.id
        assert fetched_profile.last_result_count == 23
        assert fetched_profile.last_error_code == "quota_limited"
        assert listed_profile.latest_job_id == profile_job.id
        assert listed_profile.last_result_count == 23
        assert repository.delete_profile(profile.id) is True
        assert repository.get_profile(profile.id) is None
        assert repository.get_job(profile_job.id).profile_id == profile.id

        assert repository.get_user_role(user_id).value == "editor"
        assert repository.update_set(
            set_id, SetPatch(title="Curated title")
        ).title == "Curated title"

        race_payloads = [
            RawSetPayload(
                source=SetSource.soundcloud,
                source_id="task-2-identity-race",
                canonical_url=(
                    "https://soundcloud.com/syco23/"
                    f"task-2-identity-race-{index}"
                ),
                title=f"Task 2 renamed identity race {index}",
                duration_seconds=3600 + index,
                published_at=now,
                raw_payload={},
            )
            for index in range(2)
        ]
        race_score = ScoreResult(
            score=0.8,
            accepted=True,
            auto_accept=True,
            reasons=["integration-race"],
        )
        race_jobs = [
            repository.create_job(
                url=race_payload.canonical_url,
                source=race_payload.source,
                job_type=JobType.url_import,
            )
            for race_payload in race_payloads
        ]
        race_job_ids = [item.id for item in race_jobs]
        race_claims = [
            repository.claim_job(race_job.id)
            for race_job in race_jobs
        ]
        assert all(
            claim is not None and claim.started_at is not None
            for claim in race_claims
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    repository.persist_processed_set,
                    payload=race_payloads[index],
                    score=race_score,
                    candidates=[],
                    job_id=race_job.id,
                    fingerprint=f"distinct-fingerprint-{index}",
                    claim_started_at=race_claims[index].started_at,
                )
                for index, race_job in enumerate(race_jobs)
            ]
            race_results = [future.result(timeout=10) for future in futures]
        assert race_results[0] == race_results[1]
        race_set_id = race_results[0]
        assert all(
            repository.get_job(job_id).result_set_id == race_set_id
            for job_id in race_job_ids
        )
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from import_jobs where input_url = %s", ("https://soundcloud.com/syco23/task-2",))
                if race_job_ids:
                    cursor.execute(
                        "delete from import_jobs where id = any(%s)",
                        (race_job_ids,),
                    )
                if profile_job_id is not None:
                    cursor.execute(
                        "delete from import_jobs where id = %s",
                        (profile_job_id,),
                    )
                if profile_id is not None:
                    cursor.execute(
                        "delete from search_profiles where id = %s",
                        (profile_id,),
                    )
                cursor.execute(
                    "delete from field_candidates where id in (%s, %s)",
                    (candidate_id, competing_candidate_id),
                )
                cursor.execute(
                    """
                    delete from events where id in (
                        select event_id from set_events where set_id = %s
                    )
                    """,
                    (set_id,),
                )
                cursor.execute("delete from set_artists where set_id = %s", (set_id,))
                cursor.execute("delete from sets where id = %s", (set_id,))
                if race_set_id is not None:
                    cursor.execute(
                        "delete from sets where id = %s",
                        (race_set_id,),
                    )
                if event_id is not None:
                    cursor.execute(
                        "delete from events where id = %s", (event_id,)
                    )
                cursor.execute("delete from user_roles where user_id = %s", (user_id,))
                cursor.execute("delete from auth.users where id = %s", (user_id,))
        pool.close()
