from fastapi import Request

from app.repositories.base import Repository
from app.workers.dispatch import JobDispatcher


def get_repository(request: Request) -> Repository:
    return request.app.state.repository


def get_job_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.job_dispatcher
