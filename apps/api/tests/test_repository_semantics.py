from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from time import sleep

from app.repository import InMemoryRepository
from app.schemas import (
    Candidate,
    JobStatus,
    JobType,
    SearchProfileCreate,
    SetSource,
)
from app.services.normalizer import RawSetPayload
from app.services.import_pipeline import process_payload
from app.services.heuristic import ScoreResult


def test_single_value_candidate_competes_and_reversal_clears_curated_value() -> None:
    repository = InMemoryRepository.seeded()
    record = next(iter(repository.sets.values()))
    berlin = Candidate(
        set_id=record.id,
        field_name="city",
        candidate_value="Berlin",
        confidence=0.8,
        source="test",
    )
    prague = Candidate(
        set_id=record.id,
        field_name="city",
        candidate_value="Prague",
        confidence=0.9,
        source="test",
    )
    record.city = None
    record.candidates = [berlin, prague]

    repository.decide_candidate(record.id, berlin.id, True)
    repository.decide_candidate(record.id, prague.id, True)

    assert repository.get_set(record.id).city == "Prague"
    assert repository.get_set(record.id).candidates[0].accepted is False

    repository.decide_candidate(record.id, prague.id, False)

    assert repository.get_set(record.id).city is None
    assert repository.get_set(record.id).candidates[1].accepted is False


def test_artist_relation_survives_until_last_matching_acceptance_is_rejected() -> None:
    repository = InMemoryRepository.seeded()
    record = next(iter(repository.sets.values()))
    first = Candidate(
        set_id=record.id,
        field_name="artist",
        candidate_value="SAME ARTIST",
        confidence=0.8,
        source="title",
    )
    second = Candidate(
        set_id=record.id,
        field_name="artist",
        candidate_value="SAME ARTIST",
        confidence=0.9,
        source="description",
    )
    record.artist_names = []
    record.candidates = [first, second]

    repository.decide_candidate(record.id, first.id, True)
    repository.decide_candidate(record.id, second.id, True)
    repository.decide_candidate(record.id, first.id, False)

    assert repository.get_set(record.id).artist_names == ["SAME ARTIST"]

    repository.decide_candidate(record.id, second.id, False)

    assert repository.get_set(record.id).artist_names == []


def test_profile_run_outcome_round_trips_through_exact_job_details() -> None:
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Round trip", query="round trip liveset")
    )
    job = repository.queue_profile(profile.id)
    assert job is not None

    claimed = repository.claim_job(job.id)
    assert claimed is not None and claimed.started_at is not None
    repository.finalize_profile_job(
        job.id,
        claimed.started_at,
        status=JobStatus.failed,
        next_page_token="next-page",
        result_count=23,
        discard_count=4,
        duplicate_count=5,
        error_code="quota_limited",
        error_message="quota_limited",
    )

    fetched = repository.get_profile(profile.id)
    listed = repository.list_profiles()[0]
    assert repository.jobs[job.id].details == {
        "query": "round trip liveset",
        "provider_key": "youtube",
        "capability": "discovery",
        "operation": "search",
        "parameters": {"query": "round trip liveset"},
        "last_result_count": 23,
        "last_error_code": "quota_limited",
        "result_count": 23,
        "discard_count": 4,
        "duplicate_count": 5,
    }
    assert fetched is not None
    assert fetched.latest_job_id == job.id
    assert fetched.last_result_count == 23
    assert fetched.last_error_code == "quota_limited"
    assert listed.latest_job_id == job.id
    assert listed.last_result_count == 23
    assert listed.last_error_code == "quota_limited"


def test_profile_queue_reuses_active_job() -> None:
    """Parallel run-now calls must not create overlapping active jobs."""
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="One active run", query="one active liveset")
    )

    first = repository.queue_profile(profile.id)
    second = repository.queue_profile(profile.id)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(
        [
            job
            for job in repository.jobs.values()
            if job.profile_id == profile.id
        ]
    ) == 1


def test_profile_queue_reuse_is_atomic_across_threads() -> None:
    """Concurrent run requests must serialize scan and create."""

    class SlowCreateRepository(InMemoryRepository):
        def create_job(self, **kwargs):
            sleep(0.02)
            return super().create_job(**kwargs)

    repository = SlowCreateRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Thread race", query="thread race liveset")
    )
    barrier = Barrier(8)

    def queue_once():
        barrier.wait()
        return repository.queue_profile(profile.id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = list(executor.map(lambda _: queue_once(), range(8)))

    assert all(job is not None for job in jobs)
    assert len({job.id for job in jobs}) == 1
    assert len(
        [
            job
            for job in repository.jobs.values()
            if job.profile_id == profile.id
        ]
    ) == 1


def test_out_of_order_profile_completion_updates_exact_job_only() -> None:
    """An older overlapping run must not overwrite a newer run's cursor."""
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Ordered cursor",
            query="ordered cursor liveset",
        )
    )
    older = repository.queue_profile(profile.id)
    assert older is not None
    newer = repository.create_job(
        url="youtube-search://ordered cursor liveset",
        source=SetSource.youtube,
        job_type=JobType.search_profile,
        profile_id=profile.id,
        details={"query": profile.query},
    )
    older_claim = repository.claim_job(older.id)
    assert older_claim is not None and older_claim.started_at is not None

    repository.finalize_profile_job(
        older.id,
        older_claim.started_at,
        status=JobStatus.completed,
        next_page_token="OLDER_PAGE",
        result_count=1,
        discard_count=0,
        duplicate_count=0,
        error_code=None,
        error_message=None,
    )

    stale = repository.get_profile(profile.id)
    assert repository.get_job(older.id).details["result_count"] == 1
    assert repository.get_job(newer.id).details == {
        "query": profile.query
    }
    assert stale is not None
    assert stale.next_page_token is None
    assert stale.last_run_at is None
    assert stale.latest_job_id == newer.id

    newer_claim = repository.claim_job(newer.id)
    assert newer_claim is not None and newer_claim.started_at is not None
    repository.finalize_profile_job(
        newer.id,
        newer_claim.started_at,
        status=JobStatus.completed,
        next_page_token="NEWER_PAGE",
        result_count=2,
        discard_count=0,
        duplicate_count=0,
        error_code=None,
        error_message=None,
    )

    current = repository.get_profile(profile.id)
    assert current is not None
    assert current.next_page_token == "NEWER_PAGE"
    assert current.last_run_at is not None
    assert current.last_result_count == 2
    assert current.latest_job_id == newer.id


def test_profile_child_job_is_idempotent_per_parent_and_source_id() -> None:
    """A reclaimed page must reuse its durable child instead of re-importing."""
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Replay", query="replay liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None
    payload = RawSetPayload(
        source=SetSource.youtube,
        source_id="same-video",
        canonical_url="https://www.youtube.com/watch?v=same-video",
        title="Same video liveset",
        duration_seconds=60,
        raw_payload={"id": "same-video"},
    )

    claimed = repository.claim_job(parent.id)
    assert claimed is not None and claimed.started_at is not None
    first = repository.get_or_create_child_job(
        parent.id,
        claimed.started_at,
        payload,
    )
    second = repository.get_or_create_child_job(
        parent.id,
        claimed.started_at,
        payload,
    )
    assert first is not None
    assert second is not None
    process_payload(repository, first.id, payload)
    third = repository.get_or_create_child_job(
        parent.id,
        claimed.started_at,
        payload,
    )

    assert second.id == first.id
    assert third is not None
    assert third.id == first.id
    assert third.details["profile_job_id"] == str(parent.id)
    assert third.details["source_id"] == "same-video"


def test_stale_profile_claim_cannot_checkpoint_create_or_finalize() -> None:
    """A worker superseded by reclaim must lose every mutation capability."""
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Fence", query="fence liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None
    first_claim = repository.claim_job(parent.id)
    assert first_claim is not None
    assert first_claim.started_at is not None
    payload = RawSetPayload(
        source=SetSource.youtube,
        source_id="late-video",
        canonical_url="https://www.youtube.com/watch?v=late-video",
        title="Late video liveset",
        duration_seconds=3_600,
        raw_payload={"id": "late-video"},
    )
    repository.jobs[parent.id] = first_claim.model_copy(
        update={
            "started_at": first_claim.started_at
            - timedelta(seconds=301)
        }
    )
    second_claim = repository.claim_job(parent.id)
    assert second_claim is not None
    assert second_claim.started_at is not None

    checkpointed = repository.checkpoint_profile_page(
        parent.id,
        first_claim.started_at,
        input_page_token=None,
        next_page_token="STALE_NEXT",
        payloads=[payload],
    )
    child = repository.get_or_create_child_job(
        parent.id,
        first_claim.started_at,
        payload,
    )
    finalized = repository.finalize_profile_job(
        parent.id,
        first_claim.started_at,
        status=JobStatus.completed,
        next_page_token="STALE_NEXT",
        result_count=1,
        discard_count=0,
        duplicate_count=0,
        error_code=None,
        error_message=None,
    )

    current = repository.get_job(parent.id)
    assert checkpointed is None
    assert child is None
    assert finalized is None
    assert current.status is JobStatus.processing
    assert current.started_at == second_claim.started_at
    assert "youtube_page_checkpoint" not in current.details


def test_stale_claim_cannot_complete_duplicate_or_discarded_job() -> None:
    """A reclaimed direct job must reject every old-worker completion path."""
    repository = InMemoryRepository()
    duplicate_id = next(iter(InMemoryRepository.seeded().sets))
    job = repository.create_job(
        url="https://www.youtube.com/watch?v=stale-direct",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    first_claim = repository.claim_job(job.id)
    assert first_claim is not None and first_claim.started_at is not None
    repository.jobs[job.id] = first_claim.model_copy(
        update={
            "started_at": first_claim.started_at
            - timedelta(seconds=301)
        }
    )
    second_claim = repository.claim_job(job.id)
    assert second_claim is not None and second_claim.started_at is not None

    duplicate = repository.complete_duplicate_job(
        job.id,
        duplicate_id,
        claim_started_at=first_claim.started_at,
    )
    discarded = repository.complete_discarded_job(
        job.id,
        ScoreResult(
            score=0.1,
            accepted=False,
            auto_accept=False,
            reasons=["too short"],
        ),
        claim_started_at=first_claim.started_at,
    )

    current = repository.get_job(job.id)
    assert duplicate is None
    assert discarded is None
    assert current is not None
    assert current.status is JobStatus.processing
    assert current.started_at == second_claim.started_at


def test_date_then_year_uses_one_semantic_group_and_year_starts_january_first() -> None:
    repository = InMemoryRepository.seeded()
    record = next(iter(repository.sets.values()))
    date_candidate = Candidate(
        set_id=record.id,
        field_name="date",
        candidate_value="2026-05-18",
        confidence=0.9,
        source="test",
    )
    year_candidate = Candidate(
        set_id=record.id,
        field_name="year",
        candidate_value="2027",
        confidence=0.8,
        source="test",
    )
    record.year = None
    record.raw_payload.pop("event_date", None)
    record.candidates = [date_candidate, year_candidate]

    repository.decide_candidate(record.id, date_candidate.id, True)
    repository.decide_candidate(record.id, year_candidate.id, True)
    detail = repository.get_set(record.id)

    assert detail.candidates[0].accepted is False
    assert detail.candidates[1].accepted is True
    assert detail.raw_payload["event_date"] == "2027-01-01"
    assert detail.year == 2027


def test_year_then_date_reject_clears_shared_starts_on_only_when_inactive() -> None:
    repository = InMemoryRepository.seeded()
    record = next(iter(repository.sets.values()))
    year_candidate = Candidate(
        set_id=record.id,
        field_name="year",
        candidate_value="2027",
        confidence=0.8,
        source="test",
    )
    date_candidate = Candidate(
        set_id=record.id,
        field_name="date",
        candidate_value="2028-06-23",
        confidence=0.9,
        source="test",
    )
    record.year = None
    record.raw_payload.pop("event_date", None)
    record.candidates = [year_candidate, date_candidate]

    repository.decide_candidate(record.id, year_candidate.id, True)
    repository.decide_candidate(record.id, date_candidate.id, True)
    repository.decide_candidate(record.id, year_candidate.id, False)
    active = repository.get_set(record.id)

    assert active.raw_payload["event_date"] == "2028-06-23"
    assert active.year == 2028
    assert active.candidates[0].accepted is False
    assert active.candidates[1].accepted is True

    repository.decide_candidate(record.id, date_candidate.id, False)
    cleared = repository.get_set(record.id)

    assert "event_date" not in cleared.raw_payload
    assert cleared.year is None
