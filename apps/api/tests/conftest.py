from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repository import InMemoryRepository
from app.schemas import ImportJob
from app.workers.celery_app import celery_app


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ImportJob]] = []

    def dispatch_url(self, job: ImportJob) -> None:
        self.calls.append(("url", job))

    def dispatch_profile(self, job: ImportJob) -> None:
        self.calls.append(("profile", job))

    def retry(self, job: ImportJob) -> None:
        self.calls.append(("retry", job))


@pytest.fixture
def repository() -> InMemoryRepository:
    return InMemoryRepository.seeded()


@pytest.fixture
def eager_celery() -> Iterator[object]:
    previous_always_eager = celery_app.conf.task_always_eager
    previous_eager_propagates = celery_app.conf.task_eager_propagates
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
    )
    try:
        yield celery_app
    finally:
        celery_app.conf.update(
            task_always_eager=previous_always_eager,
            task_eager_propagates=previous_eager_propagates,
        )


def _client_with_role(repository: InMemoryRepository, role: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            repository,
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
            ),
            dispatcher=RecordingDispatcher(),
        )
    ) as client:
        client.headers["X-Local-Role"] = role
        yield client


@pytest.fixture
def client_as_viewer(repository: InMemoryRepository) -> Iterator[TestClient]:
    yield from _client_with_role(repository, "viewer")


@pytest.fixture
def client_as_editor(repository: InMemoryRepository) -> Iterator[TestClient]:
    yield from _client_with_role(repository, "editor")


@pytest.fixture
def client_as_admin(repository: InMemoryRepository) -> Iterator[TestClient]:
    yield from _client_with_role(repository, "admin")


def seeded_candidate_ids(client: TestClient) -> tuple[str, str]:
    sets_response = client.get("/sets", params={"status": "inbox"})
    assert sets_response.status_code == 200
    set_id = sets_response.json()["items"][0]["id"]
    detail_response = client.get(f"/sets/{set_id}")
    assert detail_response.status_code == 200
    return set_id, detail_response.json()["candidates"][0]["id"]
