"""HTTP API: submit a job, poll a job, list pre-configured repos."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app.auth.service import ensure_job_access, require_current_user
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job, JobState, RepoSourceKind
from app.jobs.runner import JobSteps, run_implementation, run_job
from app.jobs.store import JobStore
from app.schemas.inputs import (
    ImplementRequest,
    JobCreateRequest,
    RepoChoice,
    load_preconfigured_repos,
    normalize_clarifications,
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


class LocalRepoSupport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    allowed_roots: list[str]
    allow_dirty: bool
    require_ticket_branch_match: bool


class RepoList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[RepoChoice]
    allowed_hosts: list[str]
    local_repo_support: LocalRepoSupport


def _store(request: Request) -> JobStore:
    return cast(JobStore, request.app.state.job_store)


def _steps(request: Request) -> JobSteps:
    return cast(JobSteps, request.app.state.job_steps)


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(payload: JobCreateRequest, request: Request) -> JobCreated:
    user = require_current_user(request)
    settings: Settings = get_settings()
    ticket_key = normalize_ticket(payload.ticket, settings)
    repo_url = normalize_repo(payload.repo, settings)

    job = Job.new(ticket_key=ticket_key, repo_url=repo_url, owner_user_id=user.id if user else None)
    store = _store(request)
    store.create(job)
    logger.info("job %s created: ticket=%s repo=%s", job.id, ticket_key, repo_url)

    task = asyncio.create_task(run_job(job.id, store, settings, _steps(request)))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JobCreated(job_id=job.id)


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, request: Request) -> Job:
    user = require_current_user(request)
    job = _store(request).get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND)
    ensure_job_access(job, user)
    return job


@router.post("/jobs/{job_id}/implement", response_model=JobCreated, status_code=202)
async def implement_job(
    job_id: str, request: Request, payload: ImplementRequest | None = None
) -> JobCreated:
    user = require_current_user(request)
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND)
    ensure_job_access(job, user)
    if job.state != JobState.PLAN_READY or job.plan is None:
        raise AppError(ErrorCode.IMPLEMENTATION_NOT_READY)
    if job.repo_info is None or job.repo_info.source_kind != RepoSourceKind.LOCAL:
        raise AppError(ErrorCode.IMPLEMENTATION_NOT_SUPPORTED)
    if not job.workspace_path or not Path(job.workspace_path).exists():
        raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING)

    now = datetime.now(UTC)
    job.error = None
    job.implementation_result = None
    job.implementation_diff = None
    job.implementation_usage = None
    job.validation_results = []
    job.implementation_clarifications = normalize_clarifications(
        payload.clarifications if payload is not None else None
    )
    job.implementation_approved_at = now
    job.implementation_started_at = None
    job.implementation_finished_at = None
    job.state = JobState.IMPLEMENTATION_QUEUED
    store.save(job)
    logger.info("job %s implementation approved", job.id)

    task = asyncio.create_task(run_implementation(job.id, store, get_settings(), _steps(request)))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JobCreated(job_id=job.id)


@router.get("/repos", response_model=RepoList)
async def list_repos(request: Request) -> RepoList:
    require_current_user(request)
    settings = get_settings()
    return RepoList(
        repos=load_preconfigured_repos(settings),
        allowed_hosts=settings.allowed_git_hosts,
        local_repo_support=LocalRepoSupport(
            enabled=settings.allow_local_repos,
            allowed_roots=[str(root) for root in settings.allowed_local_repo_roots],
            allow_dirty=settings.allow_dirty_local_repos,
            require_ticket_branch_match=settings.require_local_branch_ticket_match,
        ),
    )
