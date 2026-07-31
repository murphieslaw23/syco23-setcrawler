from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.database import create_pool
from app.repositories.base import Repository
from app.repository import InMemoryRepository, PostgresRepository
from app.routers import auth, candidates, health, imports, providers, search_profiles, sets, stats
from app.services.provider import build_provider_registry, get_provider_registry
from app.workers.dispatch import JobDispatcher


def create_app(
    repository: Repository | None = None,
    *,
    settings: Settings | None = None,
    dispatcher: JobDispatcher | None = None,
) -> FastAPI:
    selected_settings = settings or get_settings()
    uses_default_settings = settings is None
    pool = None
    if repository is None:
        if selected_settings.repository_mode == "memory":
            repository = InMemoryRepository.seeded()
        else:
            pool = create_pool(selected_settings.database_url)
            repository = PostgresRepository(pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            app.state.provider_registry = (
                get_provider_registry()
                if uses_default_settings
                else build_provider_registry(selected_settings)
            )
            if pool is not None:
                await to_thread.run_sync(pool.open)
                await to_thread.run_sync(pool.wait)
            yield
        finally:
            if pool is not None:
                await to_thread.run_sync(pool.close)

    app = FastAPI(
        title="SYCO23 Setcrawler API",
        version="0.2.0",
        description="Metadata-only liveset discovery and editorial review API.",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.job_dispatcher = dispatcher or JobDispatcher()
    app.state.settings = selected_settings
    app.state.database_pool = pool
    app.state.provider_registry = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=selected_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(providers.router)
    app.include_router(imports.router)
    app.include_router(sets.router)
    app.include_router(candidates.router)
    app.include_router(search_profiles.router)
    app.include_router(stats.router)
    return app


app = create_app()
