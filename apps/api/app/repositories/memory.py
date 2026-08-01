from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from app.schemas import (
    Candidate,
    CandidateCreate,
    ImportJob,
    ImportJobPage,
    ImportJobPatch,
    JobStatus,
    JobType,
    MergeCandidate,
    MergeCandidatePage,
    MergeCandidateStatus,
    MergeDecision,
    MergeDecisionAction,
    MergeScore,
    ReviewStatus,
    SearchProfile,
    SearchProfileCreate,
    SearchProfileUpdate,
    SetDetail,
    SetImage,
    SetPage,
    SetPatch,
    SetProviderSource,
    SetSource,
    SetSummary,
    UserRole,
)
from app.services.enricher import extract_field_candidates
from app.schemas.import_job import validate_job_transition
from app.repositories.base import ActiveProfileJobsError
from app.services.heuristic import HeuristicConfig, ScoreResult
from app.services.normalizer import RawSetPayload
from app.services.merge_scoring import (
    MERGE_SUGGESTION_THRESHOLD,
    score_set_merge,
)
from app.services.provider_contracts import ProviderItemPayload
from app.services.provider_sources import (
    ProviderSourceProjection,
    legacy_source_to_provider_key,
    provider_key_to_legacy_source,
    sanitize_provider_metadata,
    validate_source_projection,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


SEED_RECORDS = [
    {
        "id": _uuid(1),
        "source": SetSource.youtube,
        "source_id": "yt-murph-2026",
        "canonical_url": "https://www.youtube.com/watch?v=yt-murph-2026",
        "title": "MURPH @ SOUTH SIDE TEKNIVAL 2026",
        "description": "Recorded at Hangar 23, Berlin on 18.05.2026. Raw underground hardtek liveset.",
        "duration_seconds": 5_062,
        "published_at": datetime(2026, 5, 18, 18, 0, tzinfo=UTC),
        "set_score": 0.82,
        "review_status": ReviewStatus.inbox,
        "artist_names": ["MURPH"],
        "event_name": "South Side Teknival",
        "venue": "Hangar 23",
        "city": "Berlin",
        "year": 2026,
        "primary_image_url": "https://images.unsplash.com/photo-1524368535928-5b5e00ddc76b?auto=format&fit=crop&w=800&q=80",
    },
    {
        "id": _uuid(2),
        "source": SetSource.soundcloud,
        "source_id": "sc-k-zmk",
        "canonical_url": "https://soundcloud.com/syco23/k-zmk-free-party",
        "title": "K- - B2B ZMK — FREE PARTY SESSION",
        "description": "Recorded at La Zone Libre, Brussels.",
        "duration_seconds": 4_365,
        "published_at": datetime(2026, 5, 17, 18, 0, tzinfo=UTC),
        "set_score": 0.65,
        "review_status": ReviewStatus.inbox,
        "artist_names": ["K- -", "ZMK"],
        "event_name": "Free Party Session",
        "venue": "La Zone Libre",
        "city": "Brussels",
        "year": 2026,
        "primary_image_url": "https://images.unsplash.com/photo-1516280440614-37939bbacd81?auto=format&fit=crop&w=800&q=80",
    },
    {
        "id": _uuid(3),
        "source": SetSource.youtube,
        "source_id": "yt-23hz-ritual",
        "canonical_url": "https://www.youtube.com/watch?v=yt-23hz-ritual",
        "title": "23HZ LIVESET @ RITUAL FLOOR",
        "description": "Recorded in Dresden, Germany. 16.05.2026",
        "duration_seconds": 5_290,
        "published_at": datetime(2026, 5, 16, 18, 0, tzinfo=UTC),
        "set_score": 0.78,
        "review_status": ReviewStatus.inbox,
        "artist_names": ["23HZ"],
        "event_name": "Ritual Floor",
        "venue": None,
        "city": "Dresden",
        "year": 2026,
        "primary_image_url": "https://images.unsplash.com/photo-1514525253161-7a46d19cd819?auto=format&fit=crop&w=800&q=80",
    },
    {
        "id": _uuid(4),
        "source": SetSource.freeteknomusic,
        "source_id": "ftm-noisekraft",
        "canonical_url": "https://freeteknomusic.org/noisekraft-ground-pressure",
        "title": "NOISEKRAFT — GROUND PRESSURE LIVE MIX",
        "description": "Funktion-One outdoor recording, Netherlands.",
        "duration_seconds": 3_933,
        "published_at": datetime(2026, 5, 15, 18, 0, tzinfo=UTC),
        "set_score": 0.56,
        "review_status": ReviewStatus.inbox,
        "artist_names": ["NOISEKRAFT"],
        "event_name": "Ground Pressure",
        "venue": "Funktion-One Outdoor",
        "city": None,
        "year": 2026,
        "primary_image_url": "https://images.unsplash.com/photo-1470229722913-7c0e2dbbafd3?auto=format&fit=crop&w=800&q=80",
    },
    {
        "id": _uuid(5),
        "source": SetSource.soundcloud,
        "source_id": "sc-acid-assembly",
        "canonical_url": "https://soundcloud.com/syco23/acid-assembly",
        "title": "ACID ASSEMBLY — WAREHOUSE MIX",
        "description": "Long-form acid session from Prague.",
        "duration_seconds": 4_800,
        "published_at": datetime(2026, 5, 14, 18, 0, tzinfo=UTC),
        "set_score": 0.74,
        "review_status": ReviewStatus.accepted,
        "artist_names": ["ACID ASSEMBLY"],
        "event_name": "Warehouse Session",
        "venue": None,
        "city": "Prague",
        "year": 2026,
        "primary_image_url": None,
    },
    {
        "id": _uuid(6),
        "source": SetSource.youtube,
        "source_id": "yt-syco-transmission",
        "canonical_url": "https://www.youtube.com/watch?v=yt-syco-transmission",
        "title": "SYCO TRANSMISSION 023 — INDUSTRIAL TRIBE",
        "description": "Published SYSTEM CORRUPT transmission.",
        "duration_seconds": 7_320,
        "published_at": datetime(2026, 5, 13, 18, 0, tzinfo=UTC),
        "set_score": 0.91,
        "review_status": ReviewStatus.published,
        "artist_names": ["SYCO"],
        "event_name": "Transmission 023",
        "venue": None,
        "city": "Berlin",
        "year": 2026,
        "primary_image_url": None,
    },
]


class InMemoryRepository:
    def __init__(self) -> None:
        self.sets: dict[UUID, SetDetail] = {}
        self._provider_sources: dict[UUID, ProviderSourceProjection] = {}
        self._retained_provider_sources: dict[
            UUID, list[ProviderSourceProjection]
        ] = {}
        self._provider_items: dict[tuple[str, str], ProviderItemPayload] = {}
        self.merge_candidates: dict[UUID, MergeCandidate] = {}
        self.merge_decisions: list[MergeDecision] = []
        self.jobs: dict[UUID, ImportJob] = {}
        self.profiles: dict[UUID, SearchProfile] = {}
        self._deleted_profile_ids: set[UUID] = set()
        self.user_roles: dict[UUID, UserRole] = {}
        self.audit: list[dict] = []
        self._lock = RLock()

    @classmethod
    def seeded(cls) -> "InMemoryRepository":
        repository = cls()
        for raw in SEED_RECORDS:
            set_id = raw["id"]
            candidates = [
                Candidate(**item.model_dump(), set_id=set_id)
                for item in extract_field_candidates(raw["title"], raw["description"])
            ]
            image = SetImage(
                remote_url=raw["primary_image_url"],
                kind="thumbnail",
                attribution=f"{raw['source'].value} provider thumbnail",
                is_primary=True,
                priority=10,
            )
            now = _now()
            repository.sets[set_id] = SetDetail(
                **raw,
                raw_payload={
                    "provider": raw["source"].value,
                    "source_id": raw["source_id"],
                    "tags": ["liveset", "tekno"],
                    "channel": "SYCO23 SOURCE NETWORK",
                },
                candidates=candidates,
                images=[image],
                created_at=now,
                updated_at=now,
            )
        for set_id, record in repository.sets.items():
            repository._provider_sources[set_id] = ProviderSourceProjection(
                provider_key=legacy_source_to_provider_key(record.source),
                external_id=record.source_id,
                canonical_url=record.canonical_url,
                raw_metadata=sanitize_provider_metadata(record.raw_payload),
            )
        for name, query in (
            ("Freetekno livesets", "freetekno liveset"),
            ("Tribe B2B", "tribe b2b dj set"),
            ("Known crews", "teknival recorded at"),
        ):
            profile = SearchProfile(name=name, query=query)
            repository.profiles[profile.id] = profile
        return repository

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
        for record in self.sets.values():
            self._validate_source_projection_unlocked(record)
        records = [
            record
            for record in self.sets.values()
            if record.duplicate_of_id is None
        ]
        if source:
            records = [record for record in records if record.source == source]
        if status:
            records = [record for record in records if record.review_status == status]
        if min_score is not None:
            records = [record for record in records if record.set_score >= min_score]
        if search:
            needle = search.casefold()
            records = [
                record
                for record in records
                if needle in record.title.casefold()
                or any(needle in artist.casefold() for artist in record.artist_names)
                or (record.event_name and needle in record.event_name.casefold())
            ]
        records.sort(key=lambda item: item.published_at or item.created_at, reverse=True)
        total = len(records)
        summaries = [SetSummary(**item.model_dump()) for item in records[offset : offset + limit]]
        return SetPage(items=summaries, total=total, limit=limit, offset=offset)

    def get_set(self, set_id: UUID) -> SetDetail | None:
        record = self.sets.get(set_id)
        if record is None:
            return None
        self._validate_source_projection_unlocked(record)
        return deepcopy(record)

    def _validate_source_projection_unlocked(self, record: SetDetail) -> None:
        if record.duplicate_of_id is not None:
            return
        projection = self._provider_sources.get(record.id)
        validate_source_projection(
            legacy_source=record.source,
            legacy_external_id=record.source_id,
            provider_key=projection.provider_key if projection else None,
            provider_external_id=projection.external_id if projection else None,
            is_primary=projection.is_primary if projection else None,
        )

    def update_set(self, set_id: UUID, patch: SetPatch, actor: str = "local-editor") -> SetDetail | None:
        record = self.sets.get(set_id)
        if not record:
            return None
        changes = patch.model_dump(exclude_none=True)
        updated = record.model_copy(update={**changes, "updated_at": _now()})
        self.sets[set_id] = updated
        self.audit.append({"set_id": str(set_id), "action": "updated", "actor": actor, "details": changes})
        return deepcopy(updated)

    def decide_candidate(self, set_id: UUID, candidate_id: UUID, accepted: bool) -> Candidate | None:
        record = self.sets.get(set_id)
        if not record:
            return None
        for index, candidate in enumerate(record.candidates):
            if candidate.id == candidate_id:
                was_accepted = candidate.accepted is True
                decided = candidate.model_copy(update={"accepted": accepted})
                record.candidates[index] = decided
                if accepted and candidate.field_name in {
                    "event",
                    "date",
                    "year",
                    "venue",
                    "city",
                }:
                    affected_fields = _candidate_semantic_fields(
                        candidate.field_name
                    )
                    record.candidates = [
                        item.model_copy(update={"accepted": False})
                        if item.id != candidate_id
                        and item.field_name in affected_fields
                        else item
                        for item in record.candidates
                    ]
                    _apply_memory_candidate(record, candidate)
                elif accepted and candidate.field_name == "artist":
                    if not any(
                        name.casefold() == candidate.candidate_value.casefold()
                        for name in record.artist_names
                    ):
                        record.artist_names.append(candidate.candidate_value)
                elif not accepted and was_accepted:
                    _reverse_memory_candidate(record, candidate)
                if record.review_status == ReviewStatus.inbox:
                    record.review_status = ReviewStatus.reviewing
                record.updated_at = _now()
                self.audit.append(
                    {
                        "set_id": str(set_id),
                        "action": "candidate_accepted" if accepted else "candidate_rejected",
                        "details": {"candidate_id": str(candidate_id)},
                    }
                )
                return deepcopy(decided)
        return None

    def create_job(
        self,
        *,
        url: str | None,
        source: SetSource,
        job_type: JobType,
        profile_id: UUID | None = None,
        details: dict | None = None,
    ) -> ImportJob:
        job = ImportJob(
            url=url,
            source=source,
            job_type=job_type,
            profile_id=profile_id,
            details=details or {},
        )
        self.jobs[job.id] = job
        return deepcopy(job)

    def create_retry_job(self, job_id: UUID) -> tuple[ImportJob, bool] | None:
        """Atomically return one active retry job for a terminal parent job."""
        with self._lock:
            previous = self.jobs.get(job_id)
            if previous is None or previous.status not in {
                JobStatus.failed,
                JobStatus.dead_letter,
            }:
                return None
            retry_of = str(job_id)
            active = next(
                (
                    job
                    for job in self.jobs.values()
                    if job.details.get("retry_of_job_id") == retry_of
                    and job.status
                    in {JobStatus.queued, JobStatus.processing, JobStatus.retry}
                ),
                None,
            )
            if active is not None:
                return deepcopy(active), False
            job = ImportJob(
                url=previous.url,
                source=previous.source,
                job_type=previous.job_type,
                profile_id=previous.profile_id,
                details={
                    **previous.details,
                    "retry_of_job_id": retry_of,
                },
            )
            self.jobs[job.id] = job
            return deepcopy(job), True

    def list_jobs(
        self,
        *,
        source: SetSource | None,
        status: JobStatus | None,
        limit: int,
        offset: int,
    ) -> ImportJobPage:
        jobs = list(self.jobs.values())
        if source is not None:
            jobs = [job for job in jobs if job.source == source]
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        jobs.sort(key=lambda job: (job.created_at, str(job.id)), reverse=True)
        return ImportJobPage(
            items=[deepcopy(job) for job in jobs[offset : offset + limit]],
            total=len(jobs),
            limit=limit,
            offset=offset,
        )

    def get_job(self, job_id: UUID) -> ImportJob | None:
        job = self.jobs.get(job_id)
        return deepcopy(job) if job else None

    def claim_job(
        self,
        job_id: UUID,
        *,
        claim_ttl_seconds: int = 300,
    ) -> ImportJob | None:
        with self._lock:
            return self._claim_job_unlocked(
                job_id,
                claim_ttl_seconds=claim_ttl_seconds,
            )

    def _claim_job_unlocked(
        self,
        job_id: UUID,
        *,
        claim_ttl_seconds: int = 300,
    ) -> ImportJob | None:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        job = self.jobs.get(job_id)
        if job is None:
            return None
        now = _now()
        is_reclaim = (
            job.status is JobStatus.processing
            and job.started_at is not None
            and job.started_at
            < now - timedelta(seconds=claim_ttl_seconds)
        )
        is_due_retry = (
            job.status is JobStatus.retry
            and (
                job.next_retry_at is None
                or job.next_retry_at <= now
            )
        )
        if job.status not in {
            JobStatus.queued,
        } and not is_due_retry and not is_reclaim:
            return None
        if not is_reclaim:
            validate_job_transition(job.status, JobStatus.processing)
        details = job.details
        if is_reclaim:
            details = {
                **details,
                "reclaim_count": int(
                    details.get("reclaim_count", 0)
                )
                + 1,
                "last_reclaimed_at": now.isoformat(),
                "reclaimed_started_at": job.started_at.isoformat(),
            }
        updated = job.model_copy(
            update={
                "status": JobStatus.processing,
                "attempt_count": job.attempt_count + 1,
                "started_at": now,
                "next_retry_at": None,
                "details": details,
            }
        )
        self.jobs[job_id] = updated
        return deepcopy(updated)

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
        now = _now()
        stale_before = now - timedelta(seconds=claim_ttl_seconds)
        with self._lock:
            jobs = [
                job
                for job in self.jobs.values()
                if (
                    job.status is JobStatus.queued
                    or (
                        job.status is JobStatus.retry
                        and (
                            job.next_retry_at is None
                            or job.next_retry_at <= now
                        )
                    )
                    or (
                        job.status is JobStatus.processing
                        and job.started_at is not None
                        and job.started_at < stale_before
                    )
                )
            ]
            jobs.sort(key=lambda item: (item.created_at, str(item.id)))
            return [deepcopy(job) for job in jobs[:limit]]

    def transition_job(
        self, job_id: UUID, patch: ImportJobPatch
    ) -> ImportJob | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if patch.status is not None:
            validate_job_transition(job.status, patch.status)
        updated = job.model_copy(update=patch.model_dump(exclude_unset=True))
        self.jobs[job_id] = updated
        return deepcopy(updated)

    def transition_claimed_job(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        patch: ImportJobPatch,
    ) -> ImportJob | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status
                not in {JobStatus.processing, JobStatus.retry}
                or job.started_at != claim_started_at
            ):
                return None
            return self.transition_job(job_id, patch)

    def complete_duplicate_job(
        self,
        job_id: UUID,
        duplicate_set_id: UUID,
        *,
        claim_started_at: datetime,
    ) -> ImportJob | None:
        current = self.jobs.get(job_id)
        if current is None:
            raise KeyError(f"Import job {job_id} not found")
        job = self.transition_claimed_job(
            job_id,
            claim_started_at,
            ImportJobPatch(
                status=JobStatus.completed,
                finished_at=_now(),
                result_set_id=duplicate_set_id,
                next_retry_at=None,
                error_code=None,
                error_message=None,
                details={
                    **current.details,
                    "outcome": "duplicate",
                    "duplicate": True,
                },
            ),
        )
        return job

    def complete_discarded_job(
        self,
        job_id: UUID,
        score: ScoreResult,
        *,
        claim_started_at: datetime,
    ) -> ImportJob | None:
        current = self.jobs.get(job_id)
        if current is None:
            raise KeyError(f"Import job {job_id} not found")
        job = self.transition_claimed_job(
            job_id,
            claim_started_at,
            ImportJobPatch(
                status=JobStatus.completed,
                finished_at=_now(),
                next_retry_at=None,
                error_code=None,
                error_message=None,
                details={
                    **current.details,
                    "outcome": "discarded",
                    "score": score.score,
                    "score_reasons": score.reasons,
                },
            ),
        )
        return job

    def find_duplicate(
        self, payload: RawSetPayload, fingerprint: str
    ) -> UUID | None:
        with self._lock:
            return self._find_duplicate_unlocked(payload, fingerprint)

    def _find_duplicate_unlocked(
        self,
        payload: RawSetPayload,
        fingerprint: str,
    ) -> UUID | None:
        for record in self.sets.values():
            if (
                record.source == payload.source
                and record.source_id == payload.source_id
            ):
                return record.id
        for record in self.sets.values():
            if (
                record.source == payload.source
                and record.canonical_url == payload.canonical_url
            ):
                return record.id
        return None

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
        with self._lock:
            return self._persist_processed_set_unlocked(
                payload=payload,
                score=score,
                candidates=candidates,
                job_id=job_id,
                fingerprint=fingerprint,
                claim_started_at=claim_started_at,
            )

    def _persist_processed_set_unlocked(
        self,
        *,
        payload: RawSetPayload,
        score: ScoreResult,
        candidates: list[CandidateCreate],
        job_id: UUID,
        fingerprint: str,
        claim_started_at: datetime,
    ) -> UUID | None:
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.status is not JobStatus.processing
            or job.started_at != claim_started_at
        ):
            return None
        duplicate_id = self._find_duplicate_unlocked(
            payload,
            fingerprint,
        )
        if duplicate_id is not None:
            self._validate_source_projection_unlocked(self.sets[duplicate_id])
            self.jobs[job_id] = job.model_copy(
                update={
                    "status": JobStatus.completed,
                    "finished_at": _now(),
                    "next_retry_at": None,
                    "result_set_id": duplicate_id,
                    "error_code": None,
                    "error_message": None,
                    "details": {
                        **job.details,
                        "outcome": "duplicate",
                        "duplicate": True,
                    },
                }
            )
            return duplicate_id
        set_id = uuid4()
        now = _now()
        image = (
            [
                SetImage(
                    remote_url=payload.primary_image_url,
                    kind="thumbnail",
                    attribution=f"{payload.source.value} provider thumbnail",
                    is_primary=True,
                    priority=10,
                )
            ]
            if payload.primary_image_url
            else []
        )
        self.sets[set_id] = SetDetail(
            id=set_id,
            source=payload.source,
            source_id=payload.source_id,
            canonical_url=payload.canonical_url,
            title=payload.title,
            description=payload.description,
            duration_seconds=payload.duration_seconds,
            published_at=payload.published_at,
            set_score=score.score,
            review_status=ReviewStatus.inbox,
            score_reasons=score.reasons,
            import_job_id=job_id,
            raw_payload={
                **payload.raw_payload,
                "duplicate_fingerprint": fingerprint,
                "score_reasons": score.reasons,
                "import_job_id": str(job_id),
            },
            candidates=[
                Candidate(**candidate.model_dump(), set_id=set_id)
                for candidate in candidates
            ],
            images=image,
            primary_image_url=payload.primary_image_url,
            created_at=now,
            updated_at=now,
        )
        self._provider_sources[set_id] = ProviderSourceProjection(
            provider_key=legacy_source_to_provider_key(payload.source),
            external_id=payload.source_id,
            canonical_url=payload.canonical_url,
            raw_metadata=sanitize_provider_metadata(payload.raw_payload),
        )
        self._validate_source_projection_unlocked(self.sets[set_id])
        current = self.jobs.get(job_id)
        if (
            current is not None
            and current.status is JobStatus.processing
            and current.started_at == claim_started_at
        ):
            self.jobs[job_id] = current.model_copy(
                update={
                    "status": JobStatus.completed,
                    "finished_at": now,
                    "next_retry_at": None,
                    "result_set_id": set_id,
                    "error_code": None,
                    "error_message": None,
                    "details": {
                        **current.details,
                        "outcome": "persisted",
                    },
                }
            )
            return set_id
        self.sets.pop(set_id, None)
        self._provider_sources.pop(set_id, None)
        return None

    def get_heuristic_config(self) -> HeuristicConfig:
        return HeuristicConfig()

    def get_user_role(self, user_id: UUID) -> UserRole | None:
        return self.user_roles.get(user_id)

    def list_profiles(self) -> list[SearchProfile]:
        profiles = [
            self._profile_with_latest_job(profile)
            for profile in self.profiles.values()
            if profile.id not in self._deleted_profile_ids
        ]
        return sorted(profiles, key=lambda item: item.name)

    def create_profile(self, payload: SearchProfileCreate) -> SearchProfile:
        profile = SearchProfile(**payload.model_dump())
        self.profiles[profile.id] = profile
        return profile

    def update_profile(self, profile_id: UUID, payload: SearchProfileUpdate) -> SearchProfile | None:
        profile = self.profiles.get(profile_id)
        if not profile or profile_id in self._deleted_profile_ids:
            return None
        changes = payload.model_dump(exclude_none=True)
        if "schedule_cron" in changes or "schedule_timezone" in changes:
            changes["next_scheduled_at"] = None
        updated = profile.model_copy(update=changes)
        self.profiles[profile_id] = updated
        return updated

    def get_profile(self, profile_id: UUID) -> SearchProfile | None:
        profile = self.profiles.get(profile_id)
        return (
            self._profile_with_latest_job(profile)
            if profile and profile_id not in self._deleted_profile_ids
            else None
        )

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
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status is not JobStatus.processing
                or job.started_at != claim_started_at
            ):
                return None
            existing = job.details.get(checkpoint_key)
            if existing is not None:
                return deepcopy(job)
            checkpoint = {
                "input_page_token": input_page_token,
                "next_page_token": next_page_token,
                "source_ids": [
                    payload.source_id for payload in payloads
                ],
                "payloads": [
                    payload.model_dump(mode="json")
                    for payload in payloads
                ],
            }
            updated = job.model_copy(
                update={
                    "details": {
                        **job.details,
                        checkpoint_key: checkpoint,
                    }
                }
            )
            self.jobs[job_id] = updated
            return deepcopy(updated)

    def get_or_create_child_job(
        self,
        parent_job_id: UUID,
        claim_started_at: datetime,
        payload: RawSetPayload,
    ) -> ImportJob | None:
        with self._lock:
            parent = self.jobs.get(parent_job_id)
            if (
                parent is None
                or parent.status is not JobStatus.processing
                or parent.started_at != claim_started_at
            ):
                return None
            for job in self.jobs.values():
                if (
                    job.details.get("profile_job_id")
                    == str(parent_job_id)
                    and job.details.get("source_id")
                    == payload.source_id
                ):
                    return deepcopy(job)
            return self.create_job(
                url=payload.canonical_url,
                source=payload.source,
                job_type=JobType.url_import,
                details={
                    "profile_job_id": str(parent_job_id),
                    "source_id": payload.source_id,
                },
            )

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
        with self._lock:
            return self._finalize_profile_job_unlocked(
                job_id,
                claim_started_at,
                status=status,
                next_page_token=next_page_token,
                result_count=result_count,
                discard_count=discard_count,
                duplicate_count=duplicate_count,
                error_code=error_code,
                error_message=error_message,
            )

    def _finalize_profile_job_unlocked(
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
    ) -> ImportJob:
        if status not in {
            JobStatus.completed,
            JobStatus.failed,
            JobStatus.blocked,
        }:
            raise ValueError("Profile final status must be completed, failed, or blocked")
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"Import job {job_id} not found")
        if job.profile_id is None:
            raise ValueError(f"Import job {job_id} has no search profile")
        if (
            job.status is not JobStatus.processing
            or job.started_at != claim_started_at
        ):
            return None
        validate_job_transition(job.status, status)
        now = _now()
        updated_job = job.model_copy(
            update={
                "status": status,
                "finished_at": now,
                "error_code": error_code,
                "error_message": error_message,
                "details": {
                    **job.details,
                    "last_result_count": result_count,
                    "last_error_code": error_code,
                    "result_count": result_count,
                    "discard_count": discard_count,
                    "duplicate_count": duplicate_count,
                }
            }
        )
        self.jobs[job_id] = updated_job
        profile = self.profiles.get(job.profile_id)
        latest_job = self._latest_profile_job(job.profile_id)
        if (
            profile is not None
            and latest_job is not None
            and latest_job.id == job_id
        ):
            self.profiles[job.profile_id] = profile.model_copy(
                update={
                    "last_run_at": now,
                    "next_page_token": next_page_token,
                }
            )
        return deepcopy(updated_job)

    def complete_provider_discovery(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        *,
        provider_key: str,
        items: tuple[ProviderItemPayload, ...],
        next_cursor: str | None,
    ) -> ImportJob | None:
        if any(item.provider_key != provider_key for item in items):
            raise ValueError("provider discovery item mismatch")
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status is not JobStatus.processing
                or job.started_at != claim_started_at
            ):
                return None
            if job.profile_id is None:
                raise ValueError(f"Import job {job_id} has no search profile")
            if job.details.get("provider_key") != provider_key:
                raise ValueError("provider discovery job mismatch")
            validate_job_transition(job.status, JobStatus.completed)
            now = _now()
            for item in items:
                self._provider_items[(provider_key, item.external_id)] = item
            updated = job.model_copy(
                update={
                    "status": JobStatus.completed,
                    "finished_at": now,
                    "error_code": None,
                    "error_message": None,
                    "details": {
                        **job.details,
                        "outcome": "provider_metadata_persisted",
                        "provider_item_count": len(items),
                        "provider_external_ids": [
                            item.external_id for item in items
                        ],
                        "last_result_count": len(items),
                        "last_error_code": None,
                        "result_count": len(items),
                        "discard_count": 0,
                        "duplicate_count": 0,
                    },
                }
            )
            self.jobs[job_id] = updated
            profile = self.profiles.get(job.profile_id)
            latest_job = self._latest_profile_job(job.profile_id)
            if (
                profile is not None
                and latest_job is not None
                and latest_job.id == job_id
            ):
                self.profiles[job.profile_id] = profile.model_copy(
                    update={
                        "last_run_at": now,
                        "next_page_token": next_cursor,
                    }
                )
            return deepcopy(updated)

    def get_provider_item(
        self,
        provider_key: str,
        external_id: str,
    ) -> ProviderItemPayload | None:
        with self._lock:
            item = self._provider_items.get((provider_key, external_id))
            return deepcopy(item) if item is not None else None

    def list_set_provider_sources(
        self,
        set_id: UUID,
    ) -> list[SetProviderSource]:
        with self._lock:
            values: list[ProviderSourceProjection] = []
            primary = self._provider_sources.get(set_id)
            if primary is not None:
                values.append(primary)
            values.extend(self._retained_provider_sources.get(set_id, []))
            return [
                SetProviderSource(
                    provider_key=item.provider_key,
                    external_id=item.external_id,
                    canonical_url=item.canonical_url,
                    raw_metadata=deepcopy(item.raw_metadata),
                    is_primary=item.is_primary,
                )
                for item in values
            ]

    def _replace_provider_sources_unlocked(
        self,
        set_id: UUID,
        sources: list[SetProviderSource],
    ) -> None:
        primary = next((item for item in sources if item.is_primary), None)
        if primary is None:
            self._provider_sources.pop(set_id, None)
        else:
            self._provider_sources[set_id] = ProviderSourceProjection(
                provider_key=primary.provider_key,
                external_id=primary.external_id,
                canonical_url=primary.canonical_url,
                raw_metadata=deepcopy(primary.raw_metadata),
                is_primary=True,
            )
        retained = [
            ProviderSourceProjection(
                provider_key=item.provider_key,
                external_id=item.external_id,
                canonical_url=item.canonical_url,
                raw_metadata=deepcopy(item.raw_metadata),
                is_primary=False,
            )
            for item in sources
            if primary is None
            or (item.provider_key, item.external_id)
            != (primary.provider_key, primary.external_id)
        ]
        if retained:
            self._retained_provider_sources[set_id] = retained
        else:
            self._retained_provider_sources.pop(set_id, None)

    def create_merge_candidate(
        self,
        *,
        source_set_id: UUID,
        target_set_id: UUID,
        score: MergeScore,
    ) -> MergeCandidate:
        with self._lock:
            if source_set_id == target_set_id:
                raise ValueError("merge candidate requires two sets")
            source = self.sets.get(source_set_id)
            target = self.sets.get(target_set_id)
            if source is None or target is None:
                raise KeyError("merge candidate set not found")
            if source.duplicate_of_id is not None or target.duplicate_of_id is not None:
                raise ValueError("merged sets cannot be suggested")
            source_provider = self._provider_sources.get(source_set_id)
            target_provider = self._provider_sources.get(target_set_id)
            if (
                source_provider is None
                or target_provider is None
                or source_provider.provider_key == target_provider.provider_key
            ):
                raise ValueError("merge candidates must be cross-provider")
            pair = frozenset({source_set_id, target_set_id})
            for existing in self.merge_candidates.values():
                if frozenset(
                    {existing.source_set_id, existing.target_set_id}
                ) == pair:
                    return deepcopy(existing)
            candidate = MergeCandidate(
                source_set_id=source_set_id,
                target_set_id=target_set_id,
                score=score.score,
                component_scores=score.components,
                reasons=score.reasons,
            )
            self.merge_candidates[candidate.id] = candidate
            return deepcopy(candidate)

    def suggest_merge_candidates(
        self,
        set_id: UUID,
    ) -> list[MergeCandidate]:
        with self._lock:
            source = self.sets.get(set_id)
            source_projection = self._provider_sources.get(set_id)
            if (
                source is None
                or source.duplicate_of_id is not None
                or source_projection is None
            ):
                return []
            suggestions: list[MergeCandidate] = []
            for target in self.sets.values():
                if target.id == set_id or target.duplicate_of_id is not None:
                    continue
                target_projection = self._provider_sources.get(target.id)
                if (
                    target_projection is None
                    or target_projection.provider_key
                    == source_projection.provider_key
                ):
                    continue
                score = score_set_merge(source, target)
                if score.score < MERGE_SUGGESTION_THRESHOLD:
                    continue
                suggestions.append(
                    self.create_merge_candidate(
                        source_set_id=set_id,
                        target_set_id=target.id,
                        score=score,
                    )
                )
            return suggestions

    def list_merge_candidates(
        self,
        *,
        status: MergeCandidateStatus | None,
        limit: int,
        offset: int,
    ) -> MergeCandidatePage:
        with self._lock:
            values = [
                item
                for item in self.merge_candidates.values()
                if status is None or item.status is status
            ]
            values.sort(
                key=lambda item: (item.score, item.created_at, str(item.id)),
                reverse=True,
            )
            return MergeCandidatePage(
                items=[deepcopy(item) for item in values[offset : offset + limit]],
                total=len(values),
                limit=limit,
                offset=offset,
            )

    def get_merge_candidate(
        self,
        candidate_id: UUID,
    ) -> MergeCandidate | None:
        with self._lock:
            candidate = self.merge_candidates.get(candidate_id)
            return deepcopy(candidate) if candidate is not None else None

    def list_merge_decisions(
        self,
        candidate_id: UUID,
    ) -> list[MergeDecision]:
        with self._lock:
            return [
                deepcopy(item)
                for item in self.merge_decisions
                if item.merge_candidate_id == candidate_id
            ]

    def _merge_state_unlocked(
        self,
        candidate: MergeCandidate,
    ) -> dict[str, object]:
        source = self.sets[candidate.source_set_id]
        target = self.sets[candidate.target_set_id]
        return {
            "source_set_id": str(source.id),
            "target_set_id": str(target.id),
            "source_review_status": source.review_status.value,
            "source_duplicate_of_id": (
                str(source.duplicate_of_id)
                if source.duplicate_of_id is not None
                else None
            ),
            "source_provider_items": [
                {
                    **item.model_dump(mode="json"),
                    "raw_metadata": deepcopy(item.raw_metadata),
                }
                for item in self.list_set_provider_sources(source.id)
            ],
            "target_provider_items": [
                {
                    **item.model_dump(mode="json"),
                    "raw_metadata": deepcopy(item.raw_metadata),
                }
                for item in self.list_set_provider_sources(target.id)
            ],
        }

    def approve_merge_candidate(
        self,
        candidate_id: UUID,
        *,
        actor: str,
    ) -> MergeCandidate | None:
        with self._lock:
            candidate = self.merge_candidates.get(candidate_id)
            if candidate is None or candidate.status is not MergeCandidateStatus.pending:
                return None
            source = self.sets.get(candidate.source_set_id)
            target = self.sets.get(candidate.target_set_id)
            if (
                source is None
                or target is None
                or source.duplicate_of_id is not None
                or target.duplicate_of_id is not None
            ):
                return None
            before = self._merge_state_unlocked(candidate)
            source_sources = self.list_set_provider_sources(source.id)
            target_sources = self.list_set_provider_sources(target.id)
            target_identities = {
                (item.provider_key, item.external_id) for item in target_sources
            }
            combined = list(target_sources)
            combined.extend(
                item.model_copy(update={"is_primary": False})
                for item in source_sources
                if (item.provider_key, item.external_id) not in target_identities
            )
            self._replace_provider_sources_unlocked(target.id, combined)
            self._replace_provider_sources_unlocked(source.id, [])
            now = _now()
            self.sets[source.id] = source.model_copy(
                update={
                    "duplicate_of_id": target.id,
                    "review_status": ReviewStatus.rejected,
                    "updated_at": now,
                }
            )
            updated = candidate.model_copy(
                update={
                    "status": MergeCandidateStatus.approved,
                    "reviewed_by": actor,
                    "reviewed_at": now,
                    "updated_at": now,
                }
            )
            self.merge_candidates[candidate_id] = updated
            after = self._merge_state_unlocked(updated)
            self.merge_decisions.append(
                MergeDecision(
                    merge_candidate_id=candidate_id,
                    action=MergeDecisionAction.approve,
                    actor=actor,
                    before_state=before,
                    after_state=after,
                )
            )
            return deepcopy(updated)

    def reject_merge_candidate(
        self,
        candidate_id: UUID,
        *,
        actor: str,
    ) -> MergeCandidate | None:
        with self._lock:
            candidate = self.merge_candidates.get(candidate_id)
            if candidate is None or candidate.status is not MergeCandidateStatus.pending:
                return None
            before = self._merge_state_unlocked(candidate)
            now = _now()
            updated = candidate.model_copy(
                update={
                    "status": MergeCandidateStatus.rejected,
                    "reviewed_by": actor,
                    "reviewed_at": now,
                    "updated_at": now,
                }
            )
            self.merge_candidates[candidate_id] = updated
            self.merge_decisions.append(
                MergeDecision(
                    merge_candidate_id=candidate_id,
                    action=MergeDecisionAction.reject,
                    actor=actor,
                    before_state=before,
                    after_state=self._merge_state_unlocked(updated),
                )
            )
            return deepcopy(updated)

    def restore_merge_candidate(
        self,
        candidate_id: UUID,
        *,
        actor: str,
    ) -> MergeCandidate | None:
        with self._lock:
            candidate = self.merge_candidates.get(candidate_id)
            if candidate is None or candidate.status is not MergeCandidateStatus.approved:
                return None
            approval = next(
                (
                    item
                    for item in reversed(self.merge_decisions)
                    if item.merge_candidate_id == candidate_id
                    and item.action is MergeDecisionAction.approve
                ),
                None,
            )
            if approval is None:
                return None
            before = self._merge_state_unlocked(candidate)
            original_source = [
                SetProviderSource.model_validate(item)
                for item in approval.before_state["source_provider_items"]
            ]
            original_target = [
                SetProviderSource.model_validate(item)
                for item in approval.before_state["target_provider_items"]
            ]
            moved = {
                (item.provider_key, item.external_id) for item in original_source
            }
            target_before = {
                (item.provider_key, item.external_id) for item in original_target
            }
            current_target = self.list_set_provider_sources(
                candidate.target_set_id
            )
            restored_target = [
                item
                for item in current_target
                if (item.provider_key, item.external_id) not in moved
                or (item.provider_key, item.external_id) in target_before
            ]
            self._replace_provider_sources_unlocked(
                candidate.source_set_id,
                original_source,
            )
            self._replace_provider_sources_unlocked(
                candidate.target_set_id,
                restored_target,
            )
            source = self.sets[candidate.source_set_id]
            now = _now()
            self.sets[source.id] = source.model_copy(
                update={
                    "duplicate_of_id": None,
                    "review_status": ReviewStatus(
                        approval.before_state["source_review_status"]
                    ),
                    "updated_at": now,
                }
            )
            updated = candidate.model_copy(
                update={
                    "status": MergeCandidateStatus.restored,
                    "reviewed_by": actor,
                    "reviewed_at": now,
                    "updated_at": now,
                }
            )
            self.merge_candidates[candidate_id] = updated
            self.merge_decisions.append(
                MergeDecision(
                    merge_candidate_id=candidate_id,
                    action=MergeDecisionAction.restore,
                    actor=actor,
                    before_state=before,
                    after_state=self._merge_state_unlocked(updated),
                )
            )
            return deepcopy(updated)

    def delete_profile(self, profile_id: UUID) -> bool:
        with self._lock:
            if (
                profile_id not in self.profiles
                or profile_id in self._deleted_profile_ids
            ):
                return False
            if any(
                job.profile_id == profile_id
                and job.status
                in {
                    JobStatus.queued,
                    JobStatus.processing,
                    JobStatus.retry,
                }
                for job in self.jobs.values()
            ):
                raise ActiveProfileJobsError(
                    "Search profile has an active import job"
                )
            profile = self.profiles[profile_id]
            self.profiles[profile_id] = profile.model_copy(
                update={"enabled": False}
            )
            self._deleted_profile_ids.add(profile_id)
            return True

    def queue_profile(self, profile_id: UUID) -> ImportJob | None:
        result = self.queue_profile_with_creation(profile_id)
        return result[0] if result is not None else None

    def queue_profile_with_creation(
        self,
        profile_id: UUID,
    ) -> tuple[ImportJob, bool] | None:
        with self._lock:
            profile = self.profiles.get(profile_id)
            if (
                not profile
                or profile_id in self._deleted_profile_ids
            ):
                return None
            active = [
                job
                for job in self.jobs.values()
                if job.profile_id == profile_id
                and job.status
                in {JobStatus.queued, JobStatus.processing, JobStatus.retry}
            ]
            if active:
                return (
                    deepcopy(
                        max(
                            active,
                            key=lambda job: (
                                job.created_at,
                                str(job.id),
                            ),
                        )
                    ),
                    False,
                )
            return (
                self.create_job(
                url=f"{profile.source}-{profile.operation}://{profile.query}",
                source=provider_key_to_legacy_source(profile.source),
                job_type=JobType.search_profile,
                profile_id=profile_id,
                details={
                    "provider_key": profile.source,
                    "capability": "discovery",
                    "operation": profile.operation,
                    "parameters": profile.parameters,
                    "query": profile.query,
                },
                ),
                True,
            )

    def mark_profile_scheduled(
        self,
        profile_id: UUID,
        *,
        scheduled_at: datetime,
        next_scheduled_at: datetime,
    ) -> SearchProfile | None:
        with self._lock:
            profile = self.profiles.get(profile_id)
            if profile is None or profile_id in self._deleted_profile_ids:
                return None
            updated = profile.model_copy(
                update={
                    "last_scheduled_at": scheduled_at,
                    "next_scheduled_at": next_scheduled_at,
                }
            )
            self.profiles[profile_id] = updated
            return deepcopy(updated)

    def _latest_profile_job(self, profile_id: UUID) -> ImportJob | None:
        jobs = [
            job for job in self.jobs.values() if job.profile_id == profile_id
        ]
        return (
            max(jobs, key=lambda job: (job.created_at, str(job.id)))
            if jobs
            else None
        )

    def _profile_with_latest_job(
        self, profile: SearchProfile
    ) -> SearchProfile:
        latest_job = self._latest_profile_job(profile.id)
        if latest_job is None:
            return deepcopy(profile)
        return profile.model_copy(
            update={
                "latest_job_id": latest_job.id,
                "last_result_count": latest_job.details.get(
                    "last_result_count"
                ),
                "last_error_code": latest_job.details.get(
                    "last_error_code"
                ),
            }
        )

    def stats(self) -> dict:
        by_source = {source.value: 0 for source in SetSource}
        by_status = {status.value: 0 for status in ReviewStatus}
        active_sets = tuple(
            record
            for record in self.sets.values()
            if record.duplicate_of_id is None
        )
        for record in active_sets:
            by_source[record.source.value] += 1
            by_status[record.review_status.value] += 1
        job_counts = {status.value: 0 for status in JobStatus}
        for job in self.jobs.values():
            job_counts[job.status.value] += 1
        return {
            "total_sets": len(active_sets),
            "by_source": by_source,
            "by_status": by_status,
            "score_bands": {
                "high": sum(record.set_score >= 0.7 for record in active_sets),
                "review": sum(0.4 <= record.set_score < 0.7 for record in active_sets),
                "low": sum(record.set_score < 0.4 for record in active_sets),
            },
            "queue": {
                "queued": job_counts[JobStatus.queued.value],
                "processing": job_counts[JobStatus.processing.value],
                "failed": (
                    job_counts[JobStatus.failed.value]
                    + job_counts[JobStatus.dead_letter.value]
                ),
                "completed": job_counts[JobStatus.completed.value],
                "retry": job_counts[JobStatus.retry.value],
                "blocked": job_counts[JobStatus.blocked.value],
            },
        }

    def operational_metrics(
        self,
        *,
        claim_ttl_seconds: int,
    ) -> dict[str, int]:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        stale_before = _now() - timedelta(seconds=claim_ttl_seconds)
        jobs = tuple(self.jobs.values())
        return {
            "dead_letter_jobs": sum(
                job.status is JobStatus.dead_letter for job in jobs
            ),
            "stuck_processing_jobs": sum(
                job.status is JobStatus.processing
                and job.started_at is not None
                and job.started_at < stale_before
                for job in jobs
            ),
            "provider_quota_failures": sum(
                job.error_code == "youtube_quota_exceeded" for job in jobs
            ),
            "provider_robots_failures": sum(
                job.error_code == "robots_denied" for job in jobs
            ),
        }


def _apply_memory_candidate(
    record: SetDetail, candidate: Candidate
) -> None:
    field_map = {
        "event": "event_name",
        "venue": "venue",
        "city": "city",
    }
    if candidate.field_name in field_map:
        setattr(
            record,
            field_map[candidate.field_name],
            candidate.candidate_value,
        )
    elif candidate.field_name == "year":
        record.year = int(candidate.candidate_value)
        record.raw_payload["event_date"] = (
            f"{candidate.candidate_value}-01-01"
        )
    elif candidate.field_name == "date":
        record.raw_payload["event_date"] = candidate.candidate_value
        record.year = int(candidate.candidate_value[:4])


def _reverse_memory_candidate(
    record: SetDetail, candidate: Candidate
) -> None:
    if candidate.field_name == "artist":
        has_other = any(
            item.id != candidate.id
            and item.field_name == "artist"
            and item.accepted is True
            and item.candidate_value.casefold()
            == candidate.candidate_value.casefold()
            for item in record.candidates
        )
        if not has_other:
            record.artist_names = [
                name
                for name in record.artist_names
                if name.casefold() != candidate.candidate_value.casefold()
            ]
        return
    if candidate.field_name in {"date", "year"}:
        has_other = any(
            item.id != candidate.id
            and item.field_name in {"date", "year"}
            and item.accepted is True
            for item in record.candidates
        )
        if has_other:
            return
        active_date = record.raw_payload.get("event_date")
        matches = (
            candidate.field_name == "date"
            and active_date == candidate.candidate_value
        ) or (
            candidate.field_name == "year"
            and active_date == f"{candidate.candidate_value}-01-01"
        )
        if matches:
            record.raw_payload.pop("event_date", None)
            record.year = None
        return
    field_map = {
        "event": "event_name",
        "venue": "venue",
        "city": "city",
    }
    if candidate.field_name in field_map:
        attribute = field_map[candidate.field_name]
        current = getattr(record, attribute)
        if (
            current is not None
            and str(current).casefold()
            == candidate.candidate_value.casefold()
        ):
            setattr(record, attribute, None)


def _candidate_semantic_fields(field_name: str) -> frozenset[str]:
    if field_name in {"date", "year"}:
        return frozenset({"date", "year"})
    return frozenset({field_name})
