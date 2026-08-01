import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from celery.exceptions import Retry
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repositories.memory import InMemoryRepository
from app.schemas import (
    ImportJobPatch,
    JobStatus,
    JobType,
    SearchProfileCreate,
    SetSource,
)
from app.services.enricher import extract_field_candidates
from app.services.import_pipeline import process_payload
from app.services.normalizer import RawSetPayload


ROOT = Path(__file__).resolve().parents[3]


def _payload(
    source: SetSource,
    source_id: str,
    *,
    canonical_url: str,
    title: str = "SYCO23 LIVESET @ RELEASE TEST",
    duration_seconds: int = 5_400,
) -> RawSetPayload:
    return RawSetPayload(
        source=source,
        source_id=source_id,
        canonical_url=canonical_url,
        title=title,
        description="Recorded in Berlin.",
        duration_seconds=duration_seconds,
        raw_payload={"provider": source.value},
    )


def test_source_identity_wins_after_provider_metadata_is_renamed() -> None:
    """Dropping source/source_id lookup would persist renamed provider metadata twice."""
    repository = InMemoryRepository()
    original = _payload(
        SetSource.soundcloud,
        "provider-object-23",
        canonical_url="https://soundcloud.com/syco23/original-name",
    )
    renamed = _payload(
        SetSource.soundcloud,
        "provider-object-23",
        canonical_url="https://soundcloud.com/syco23/renamed-object",
        title="RENAMED RECORDING @ DIFFERENT EVENT",
        duration_seconds=6_123,
    )
    first_job = repository.create_job(
        url=original.canonical_url,
        source=original.source,
        job_type=JobType.url_import,
    )
    second_job = repository.create_job(
        url=renamed.canonical_url,
        source=renamed.source,
        job_type=JobType.url_import,
    )

    first = process_payload(repository, first_job.id, original)
    second = process_payload(repository, second_job.id, renamed)

    assert first is not None
    assert second == first
    assert len(repository.sets) == 1
    assert repository.get_job(second_job.id).details["outcome"] == "duplicate"


def test_competing_renamed_payloads_share_one_source_identity() -> None:
    """Removing the in-lock source identity recheck would admit competing rows."""
    repository = InMemoryRepository()
    payloads = [
        _payload(
            SetSource.youtube,
            "same-video-id",
            canonical_url=f"https://www.youtube.com/watch?v=canonical-{index}",
            title=f"COMPETING TITLE {index} LIVESET",
            duration_seconds=4_000 + index,
        )
        for index in range(2)
    ]
    jobs = [
        repository.create_job(
            url=payload.canonical_url,
            source=payload.source,
            job_type=JobType.url_import,
        )
        for payload in payloads
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: process_payload(
                    repository,
                    pair[0].id,
                    pair[1],
                ),
                zip(jobs, payloads, strict=True),
            )
        )

    assert results[0] == results[1]
    assert len(repository.sets) == 1
    assert sorted(
        repository.get_job(job.id).details["outcome"] for job in jobs
    ) == ["duplicate", "persisted"]


def test_celery_rejects_worker_loss_and_failed_late_ack_tasks() -> None:
    """A lost worker or failed recovery publish must leave a broker delivery."""
    from app.workers.celery_app import celery_app

    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.task_acks_on_failure_or_timeout is False


class FailingDispatcher:
    def dispatch_url(self, _job) -> None:
        raise ConnectionError("redis password and host must stay private")

    def dispatch_profile(self, _job) -> None:
        raise ConnectionError("redis password and host must stay private")

    def retry(self, _job) -> None:
        raise ConnectionError("redis password and host must stay private")


def _live_settings() -> Settings:
    return Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="live",
        youtube_api_key="fixture-youtube-key",
    )


def test_url_dispatch_failure_terminalizes_durable_job_before_503() -> None:
    """Returning an API error with a queued row would create undispatched work."""
    repository = InMemoryRepository()
    client = TestClient(
        create_app(
            repository,
            settings=_live_settings(),
            dispatcher=FailingDispatcher(),
        )
    )

    response = client.post(
        "/imports/url",
        json={"url": "https://soundcloud.com/syco23/release-fix"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Import dispatch is temporarily unavailable"
    job = next(iter(repository.jobs.values()))
    assert job.status is JobStatus.failed
    assert job.error_code == "broker_dispatch_failed"
    assert "redis" not in (job.error_message or "").casefold()


def test_profile_dispatch_failure_terminalizes_new_durable_job_before_503() -> None:
    """A failed profile publish must be visible and terminal, not queued forever."""
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Dispatch fail", query="dispatch fail liveset")
    )
    client = TestClient(
        create_app(
            repository,
            settings=_live_settings(),
            dispatcher=FailingDispatcher(),
        )
    )

    response = client.post(f"/search-profiles/{profile.id}/run")

    assert response.status_code == 503
    job = next(iter(repository.jobs.values()))
    assert job.profile_id == profile.id
    assert job.status is JobStatus.failed
    assert job.error_code == "broker_dispatch_failed"


@pytest.mark.parametrize(
    ("module_name", "task_name", "source", "url", "profile"),
    [
        (
            "soundcloud_importer",
            "import_soundcloud",
            SetSource.soundcloud,
            "https://soundcloud.com/syco23/lease",
            False,
        ),
        (
            "ftm_scraper",
            "import_ftm",
            SetSource.freeteknomusic,
            "https://freeteknomusic.org/sets/lease",
            False,
        ),
        (
            "youtube_poller",
            "import_url",
            SetSource.youtube,
            "https://www.youtube.com/watch?v=lease",
            False,
        ),
        (
            "youtube_poller",
            "run_youtube_profile",
            SetSource.youtube,
            "youtube-search://lease",
            True,
        ),
    ],
)
def test_early_provider_redelivery_schedules_lease_expiry_without_traffic(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    task_name: str,
    source: SetSource,
    url: str,
    profile: bool,
) -> None:
    """Acknowledging an early replacement delivery would strand the live lease."""
    module = __import__(
        f"app.workers.{module_name}",
        fromlist=[module_name],
    )
    repository = InMemoryRepository()
    if profile:
        saved = repository.create_profile(
            SearchProfileCreate(name="Lease profile", query="lease liveset")
        )
        job = repository.queue_profile(saved.id)
        assert job is not None
    else:
        job = repository.create_job(
            url=url,
            source=source,
            job_type=JobType.url_import,
        )
    owner = repository.claim_job(job.id)
    assert owner is not None
    task = getattr(module, task_name)
    scheduled: list[tuple[tuple[str, ...], int]] = []

    monkeypatch.setattr(module, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(
        task,
        "apply_async",
        lambda args, countdown: scheduled.append((tuple(args), countdown)),
    )
    if module_name == "soundcloud_importer":
        monkeypatch.setattr(
            module,
            "get_soundcloud_adapter",
            lambda: pytest.fail("early delivery fetched SoundCloud"),
        )
    elif module_name == "ftm_scraper":
        monkeypatch.setattr(
            module,
            "get_ftm_adapter",
            lambda: pytest.fail("early delivery fetched FTM"),
        )
    else:
        monkeypatch.setattr(
            module,
            "get_youtube_adapter",
            lambda: pytest.fail("early delivery fetched YouTube"),
        )

    result = task.run(str(job.id))

    assert result is None
    assert len(scheduled) == 1
    assert scheduled[0][0] == (str(job.id),)
    assert 1 <= scheduled[0][1] <= 300
    current = repository.get_job(job.id)
    assert current.status is JobStatus.processing
    assert current.started_at == owner.started_at
    assert current.attempt_count == 1


@pytest.mark.parametrize(
    ("module_name", "task_name", "source", "url"),
    [
        (
            "soundcloud_importer",
            "import_soundcloud",
            SetSource.soundcloud,
            "https://soundcloud.com/syco23/process-queue",
        ),
        (
            "ftm_scraper",
            "import_ftm",
            SetSource.freeteknomusic,
            "https://freeteknomusic.org/sets/process-queue",
        ),
        (
            "youtube_poller",
            "import_url",
            SetSource.youtube,
            "https://www.youtube.com/watch?v=process-queue",
        ),
    ],
)
def test_direct_provider_hands_payload_and_exact_claim_to_process_queue(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    task_name: str,
    source: SetSource,
    url: str,
) -> None:
    """Calling the pipeline inline would bypass the configured process worker."""
    module = __import__(
        f"app.workers.{module_name}",
        fromlist=[module_name],
    )
    repository = InMemoryRepository()
    payload = _payload(
        source,
        "process-queue-id",
        canonical_url=url,
    )
    job = repository.create_job(
        url=url,
        source=source,
        job_type=JobType.url_import,
    )
    captured: list[tuple[UUID, RawSetPayload, datetime]] = []

    class Adapter:
        async def fetch(self, _url: str) -> RawSetPayload:
            return payload

    monkeypatch.setattr(module, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(
        module,
        "process_payload",
        lambda *args, **kwargs: pytest.fail("provider persisted inline"),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "dispatch_process_payload",
        lambda job_id, raw_payload, claim_started_at: captured.append(
            (job_id, raw_payload, claim_started_at)
        ),
        raising=False,
    )
    if module_name == "soundcloud_importer":
        monkeypatch.setattr(module, "get_soundcloud_adapter", lambda: Adapter())
    elif module_name == "ftm_scraper":
        monkeypatch.setattr(module, "get_ftm_adapter", lambda: Adapter())
    else:
        monkeypatch.setattr(module, "get_youtube_adapter", lambda: Adapter())
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            ftm_scraper_enabled=True,
        ),
    )

    result = getattr(module, task_name).run(str(job.id))

    assert result is None
    assert len(captured) == 1
    queued_job_id, queued_payload, owner_token = captured[0]
    current = repository.get_job(job.id)
    assert queued_job_id == job.id
    assert queued_payload == payload
    assert owner_token == current.started_at
    assert current.status is JobStatus.processing
    assert repository.sets == {}


def test_process_task_requires_and_fences_serialized_claim_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the serialized owner token would let a stale process task persist."""
    from app.workers import normalize_worker

    repository = InMemoryRepository()
    payload = _payload(
        SetSource.soundcloud,
        "token-fence",
        canonical_url="https://soundcloud.com/syco23/token-fence",
    )
    job = repository.create_job(
        url=payload.canonical_url,
        source=payload.source,
        job_type=JobType.url_import,
    )
    first = repository.claim_job(job.id)
    assert first is not None and first.started_at is not None
    repository.jobs[job.id] = first.model_copy(
        update={"started_at": first.started_at.replace(year=2025)}
    )
    replacement = repository.claim_job(job.id, claim_ttl_seconds=1)
    assert replacement is not None
    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )

    result = normalize_worker.process_raw_payload.run(
        str(job.id),
        payload.model_dump(mode="json"),
        first.started_at.isoformat(),
    )

    assert result is None
    current = repository.get_job(job.id)
    assert current.status is JobStatus.processing
    assert current.started_at == replacement.started_at
    assert repository.sets == {}


def test_youtube_profile_dispatches_durable_children_without_inline_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inlining profile children would make the parent counts and process queue false."""
    from app.services.youtube import YouTubeSearchBatch
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Child queue", query="child queue liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None
    payloads = [
        _payload(
            SetSource.youtube,
            f"child-{index}",
            canonical_url=(
                f"https://www.youtube.com/watch?v=child-{index}"
            ),
        )
        for index in range(2)
    ]
    dispatched: list[tuple[UUID, datetime]] = []
    finalizers: list[tuple[str, str]] = []

    class Adapter:
        async def search(self, _profile):
            return YouTubeSearchBatch(
                payloads=payloads,
                next_page_token="NEXT_CHILD_PAGE",
            )

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_settings",
        lambda: _live_settings(),
    )
    monkeypatch.setattr(
        youtube_poller,
        "process_payload",
        lambda *args, **kwargs: pytest.fail("profile child persisted inline"),
        raising=False,
    )
    monkeypatch.setattr(
        youtube_poller,
        "dispatch_process_payload",
        lambda job_id, _payload, token: dispatched.append(
            (job_id, token)
        ),
    )
    monkeypatch.setattr(
        youtube_poller.finalize_youtube_profile,
        "apply_async",
        lambda args: finalizers.append(tuple(args)),
    )

    result = youtube_poller.run_youtube_profile.run(str(parent.id))

    assert result is None
    current_parent = repository.get_job(parent.id)
    assert current_parent.status is JobStatus.processing
    assert current_parent.details["youtube_page_checkpoint"][
        "next_page_token"
    ] == "NEXT_CHILD_PAGE"
    children = [
        job
        for job in repository.jobs.values()
        if job.details.get("profile_job_id") == str(parent.id)
    ]
    assert len(children) == 2
    assert all(child.status is JobStatus.processing for child in children)
    assert {job_id for job_id, _token in dispatched} == {
        child.id for child in children
    }
    assert all(
        repository.get_job(job_id).started_at == token
        for job_id, token in dispatched
    )
    assert finalizers == [
        (str(parent.id), current_parent.started_at.isoformat())
    ]
    assert repository.sets == {}


@pytest.mark.parametrize(
    ("module_name", "task_name", "source", "url"),
    [
        (
            "soundcloud_importer",
            "import_soundcloud",
            SetSource.soundcloud,
            "https://soundcloud.com/syco23/dispatch-fail",
        ),
        (
            "ftm_scraper",
            "import_ftm",
            SetSource.freeteknomusic,
            "https://freeteknomusic.org/sets/dispatch-fail",
        ),
        (
            "youtube_poller",
            "import_url",
            SetSource.youtube,
            "https://www.youtube.com/watch?v=dispatch-fail",
        ),
    ],
)
def test_provider_process_dispatch_failure_is_durable_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    task_name: str,
    source: SetSource,
    url: str,
) -> None:
    """A process broker outage must not leave fetched work stuck processing."""
    module = __import__(
        f"app.workers.{module_name}",
        fromlist=[module_name],
    )
    repository = InMemoryRepository()
    payload = _payload(
        source,
        "dispatch-fail",
        canonical_url=url,
    )
    job = repository.create_job(
        url=url,
        source=source,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, _url: str) -> RawSetPayload:
            return payload

    monkeypatch.setattr(module, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(
        module,
        "dispatch_process_payload",
        lambda *_args: (_ for _ in ()).throw(
            ConnectionError("private broker address")
        ),
    )
    if module_name == "soundcloud_importer":
        monkeypatch.setattr(module, "get_soundcloud_adapter", lambda: Adapter())
    elif module_name == "ftm_scraper":
        monkeypatch.setattr(module, "get_ftm_adapter", lambda: Adapter())
    else:
        monkeypatch.setattr(module, "get_youtube_adapter", lambda: Adapter())
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            ftm_scraper_enabled=True,
        ),
    )
    task = getattr(module, task_name)
    task.push_request(
        retries=0,
        called_directly=False,
        is_eager=True,
    )
    try:
        with pytest.raises(Retry) as retry:
            task.run(str(job.id))
    finally:
        task.pop_request()

    current = repository.get_job(job.id)
    assert retry.value.when == 5
    assert current.status is JobStatus.retry
    assert current.error_code == "process_dispatch_error"
    assert current.error_message == "Process queue dispatch failed"
    assert current.next_retry_at is not None


def test_ftm_paces_every_http_request_after_the_first() -> None:
    """Robots-to-page and page-to-next-robots bursts violate request pacing."""
    from app.services.ftm import FTMAdapter

    events: list[str] = []
    html = """
    <html><head>
      <meta property="og:title" content="PACE LIVESET">
      <link rel="canonical" href="https://freeteknomusic.org/sets/pace">
    </head><body><h1>PACE LIVESET</h1></body></html>
    """

    async def respond(request: httpx.Request) -> httpx.Response:
        events.append(f"request:{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nAllow: /",
                request=request,
            )
        return httpx.Response(200, text=html, request=request)

    async def sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")

    adapter = FTMAdapter(
        enabled=True,
        transport=httpx.MockTransport(respond),
        scraper_request_delay_ms=5_000,
        scraper_user_agent="syco23-test/1.0",
        sleep=sleep,
    )

    asyncio.run(adapter.fetch("https://freeteknomusic.org/sets/pace"))

    assert events == [
        "request:/robots.txt",
        "sleep:5.0",
        "request:/sets/pace",
    ]


def test_profile_delete_conflicts_with_active_job_then_soft_deletes_terminal() -> None:
    """Physical deletion or SET NULL would orphan an active or historical job."""
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Delete contract", query="delete liveset")
    )
    job = repository.queue_profile(profile.id)
    assert job is not None
    client = TestClient(create_app(repository, settings=_live_settings()))

    active = client.delete(f"/search-profiles/{profile.id}")

    assert active.status_code == 409
    assert active.json()["detail"] == "Search profile has an active import job"
    assert repository.get_job(job.id).profile_id == profile.id

    repository.transition_job(
        job.id,
        ImportJobPatch(
            status=JobStatus.failed,
            error_code="test_terminal",
        ),
    )
    terminal = client.delete(f"/search-profiles/{profile.id}")

    assert terminal.status_code == 204
    assert repository.get_profile(profile.id) is None
    assert repository.get_job(job.id).profile_id == profile.id


def test_all_search_profile_management_policies_are_admin_only() -> None:
    """Restoring editor authority in any migration would bypass the API role model."""
    migrations = [
        ROOT / "supabase/migrations/0002_rls.sql",
        ROOT / "supabase/migrations/20260728192205_provider_jobs.sql",
        ROOT / "supabase/migrations/20260729060000_final_release_fixes.sql",
    ]
    for migration in migrations:
        sql = migration.read_text().casefold()
        policy_start = sql.index('create policy "admins manage profiles"')
        policy_end = sql.index(";", policy_start)
        policy = sql[policy_start:policy_end]
        assert "has_role('admin')" in policy
        assert "has_role('editor')" not in policy


def test_data_api_security_migration_covers_every_public_table() -> None:
    """Every exposed table needs explicit grants and RLS on new Supabase projects."""
    migration = (
        ROOT / "supabase/migrations/20260729151000_data_api_security.sql"
    ).read_text().casefold()

    for table in (
        "sets",
        "artists",
        "events",
        "crews",
        "images",
        "set_artists",
        "set_events",
        "set_crews",
        "set_images",
        "field_candidates",
        "import_log",
        "search_profiles",
        "user_roles",
        "heuristic_config",
        "import_jobs",
        "provider_cursors",
    ):
        assert f"alter table public.{table} enable row level security" in migration
        assert f"revoke all on table public.{table}" in migration

    assert "grant select on table public.sets to anon" in migration
    assert "grant select, insert, update, delete" in migration
    assert "to authenticated" in migration
    assert "to service_role" in migration
    assert 'create policy "public read published set artists"' in migration
    assert 'create policy "editors manage set artists"' in migration
    assert 'create policy "editors read import log"' in migration


def test_init_only_creates_auth_users_for_plain_postgres() -> None:
    """Hosted Supabase owns auth.users and rejects application DDL in that schema."""
    migration = (ROOT / "supabase/migrations/0001_init.sql").read_text().casefold()

    assert "if to_regclass('auth.users') is null then" in migration
    assert migration.index("if to_regclass('auth.users') is null then") < migration.index(
        "create table auth.users"
    )


def test_advisor_fix_migration_locks_function_and_indexes_foreign_keys() -> None:
    """Production migrations should clear actionable security and FK advisor findings."""
    migration = (
        ROOT / "supabase/migrations/20260729152500_advisor_fixes.sql"
    ).read_text().casefold()

    assert "alter function public.set_updated_at() set search_path = ''" in migration
    for index in (
        "artists_image_id_idx",
        "crews_image_id_idx",
        "events_flyer_image_id_idx",
        "import_jobs_result_set_id_idx",
        "import_log_set_id_idx",
        "set_artists_artist_id_idx",
        "set_crews_crew_id_idx",
        "set_events_event_id_idx",
        "set_images_image_id_idx",
    ):
        assert f"create index {index}" in migration

    assert 'drop policy if exists "admins all sets"' in migration
    assert 'create policy "admins insert sets"' in migration
    assert 'create policy "admins delete sets"' in migration


def test_storage_bucket_migration_creates_private_image_buckets() -> None:
    """Provider artwork must stay private and reject oversized/non-image uploads."""
    migration = (
        ROOT / "supabase/migrations/20260729154000_storage_buckets.sql"
    ).read_text().casefold()
    compact = " ".join(migration.split())

    for bucket in ("flyers", "thumbnails", "artist-images"):
        assert f"( '{bucket}', '{bucket}', false," in compact
    assert "20971520" in migration
    assert "image/jpeg" in migration
    assert "image/png" in migration
    assert "image/webp" in migration


def test_invalid_optional_date_is_ignored_during_candidate_extraction() -> None:
    """Malformed optional provider evidence must not abort a valid import."""
    candidates = extract_field_candidates(
        "SYCO23 LIVESET",
        "Recorded in Berlin on 31.02.2026.",
    )

    assert not any(candidate.field_name == "date" for candidate in candidates)
    assert any(candidate.field_name == "year" for candidate in candidates)


def test_successful_retry_clears_stale_job_error_fields() -> None:
    """A completed retry must not continue displaying its earlier failure."""
    repository = InMemoryRepository()
    payload = _payload(
        SetSource.soundcloud,
        "retry-success",
        canonical_url="https://soundcloud.com/syco23/retry-success",
    )
    job = repository.create_job(
        url=payload.canonical_url,
        source=payload.source,
        job_type=JobType.url_import,
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None
    repository.transition_claimed_job(
        job.id,
        claimed.started_at,
        ImportJobPatch(
            status=JobStatus.retry,
            error_code="processing_error",
            error_message="temporary failure",
        ),
    )

    set_id = process_payload(repository, job.id, payload)

    completed = repository.get_job(job.id)
    assert set_id is not None
    assert completed.status is JobStatus.completed
    assert completed.error_code is None
    assert completed.error_message is None
    assert completed.next_retry_at is None
