"""HTTP API: submit a job, poll a job, list pre-configured repos."""

from __future__ import annotations

import asyncio
from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job
from app.jobs.runner import JobSteps, run_job
from app.jobs.store import JobStore
from app.schemas.inputs import (
    JobCreateRequest,
    RepoChoice,
    load_preconfigured_repos,
    normalize_repo,
    normalize_ticket,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api")

# Strong references to in-flight job tasks (asyncio only keeps weak ones).
_background_tasks: set[asyncio.Task[None]] = set()


class JobCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str


class RepoList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[RepoChoice]
    allowed_hosts: list[str]


def _store(request: Request) -> JobStore:
    return cast(JobStore, request.app.state.job_store)


def _steps(request: Request) -> JobSteps:
    return cast(JobSteps, request.app.state.job_steps)


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(payload: JobCreateRequest, request: Request) -> JobCreated:
    settings: Settings = get_settings()
    ticket_key = normalize_ticket(payload.ticket, settings)
    repo_url = normalize_repo(payload.repo, settings)

    job = Job.new(ticket_key=ticket_key, repo_url=repo_url)
    store = _store(request)
    store.create(job)
    logger.info("job %s created: ticket=%s repo=%s", job.id, ticket_key, repo_url)

    task = asyncio.create_task(run_job(job.id, store, settings, _steps(request)))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JobCreated(job_id=job.id)


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, request: Request) -> Job:
    job = _store(request).get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND)
    return job


@router.get("/repos", response_model=RepoList)
async def list_repos() -> RepoList:
    settings = get_settings()
    return RepoList(
        repos=load_preconfigured_repos(settings),
        allowed_hosts=settings.allowed_git_hosts,
    )
