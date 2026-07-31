from collections.abc import Iterator
from uuid import UUID

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


@pytest.fixture(autouse=True)
def provider_source_repository_fake_rows(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Teach the legacy SQL fake about the v0.3 source-write statements.

    Real PostgreSQL behavior remains covered by the integration suite. This
    adapter only extends the narrow cursor fake used by
    test_postgres_repository_unit without weakening production integrity
    checks or duplicating repository implementation in the tests.
    """

    if request.module.__name__ != "test_postgres_repository_unit":
        yield
        return

    cursor_type = getattr(request.module, "FakeCursor", None)
    if cursor_type is None:
        yield
        return

    original_execute = cursor_type.execute

    def execute_with_provider_rows(self, statement: str, params: object = None):
        result = original_execute(self, statement, params)
        normalized = " ".join(statement.split()).casefold()
        if "select id from providers where key" in normalized:
            self._row = {
                "id": UUID("00000000-0000-4000-8000-000000030003")
            }
        elif "insert into provider_items" in normalized:
            self._row = {
                "id": UUID("00000000-0000-4000-8000-000000030004")
            }
        elif "providers.key as provider_key" in normalized:
            self._row = {
                "source": "soundcloud",
                "source_id": "atomic-persist",
                "provider_key": "soundcloud",
                "provider_external_id": "atomic-persist",
                "is_primary": True,
            }
        return result

    monkeypatch.setattr(cursor_type, "execute", execute_with_provider_rows)
    yield


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
