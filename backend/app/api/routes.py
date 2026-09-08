"""HTTP API: submit a job, poll a job, list pre-configured repos."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from app.auth.models import UserRole
from app.auth.service import ensure_job_access, require_admin, require_current_user
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job, JobState, RepoSourceKind, RequirementSource
from app.jobs.runner import JobSteps, run_implementation, run_job, run_validation_correction
from app.jobs.store import JobStore
from app.schemas.inputs import (
    JOB_CREATE_FORM_FIELDS,
    CreateBranchRequest,
    ImplementRequest,
    PushBranchRequest,
    RepoChoice,
    load_preconfigured_repos,
    normalize_branch_name,
    normalize_clarifications,
    normalize_commit_message,
    normalize_planning_notes,
    normalize_repo,
    normalize_ticket,
    reject_unknown_form_fields,
    require_exactly_one_requirement_source,
    validate_uploaded_document,
)
from app.steps.document_fetch import requirement_document_path
from app.steps.validation_runner import correctable_failures

logger = get_logger(__name__)

router = APIRouter(prefix="/api")

# Strong references to in-flight job tasks (asyncio only keeps weak ones).
_background_tasks: set[asyncio.Task[None]] = set()


class JobCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str


class BranchCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_name: str
    commit_sha: str


class BranchPushed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_name: str
    remote_url: str
    # A plain github.com web link (not an API call — never opens or creates
    # anything itself) so the user can open a PR in one click if they want
    # one; opening a PR is still out of scope for this app to do directly.
    compare_url: str | None = None


class LocalRepoSupport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    allowed_roots: list[str]
    allow_dirty: bool
    require_ticket_branch_match: bool
    allow_non_git_folders: bool


class RepoList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[RepoChoice]
    allowed_hosts: list[str]
    local_repo_support: LocalRepoSupport


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_key: str
    repo_url: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: list[JobSummary]
    next_cursor: str | None


class OwnerCostSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None
    email: str | None
    display_name: str | None
    job_count: int
    planning_cost_usd: float
    implementation_cost_usd: float
    total_cost_usd: float


class CostSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owners: list[OwnerCostSummary]
    grand_total_usd: float


def _store(request: Request) -> JobStore:
    return cast(JobStore, request.app.state.job_store)


def _steps(request: Request) -> JobSteps:
    return cast(JobSteps, request.app.state.job_steps)


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    request: Request,
    ticket: str | None = Form(default=None, max_length=2000),
    repo: str = Form(..., min_length=1, max_length=2000),
    planning_notes: str | None = Form(default=None, max_length=4000),
    requirement_document: UploadFile | None = File(default=None),  # noqa: B008
) -> JobCreated:
    user = require_current_user(request)
    settings: Settings = get_settings()
    form = await request.form()
    reject_unknown_form_fields(set(form.keys()), JOB_CREATE_FORM_FIELDS)
    require_exactly_one_requirement_source(ticket, requirement_document is not None)
    repo_url = normalize_repo(repo, settings)
    planning_notes_normalized = normalize_planning_notes(planning_notes)

    document_content: bytes | None = None
    if requirement_document is not None:
        document_content = await requirement_document.read()
        validate_uploaded_document(document_content, settings)
        ticket_key = f"DOC-{uuid.uuid4().hex[:8].upper()}"
        requirement_source = RequirementSource.DOCUMENT
        requirement_document_name = requirement_document.filename or "requirement.pdf"
    elif ticket:
        ticket_key = normalize_ticket(ticket, settings)
        requirement_source = RequirementSource.JIRA
        requirement_document_name = None
    else:  # pragma: no cover - require_exactly_one_requirement_source already raised
        raise AppError(ErrorCode.INPUT_INVALID)

    job = Job.new(
        ticket_key=ticket_key,
        repo_url=repo_url,
        owner_user_id=user.id if user else None,
        planning_notes=planning_notes_normalized,
        requirement_source=requirement_source,
        requirement_document_name=requirement_document_name,
    )

    if document_content is not None:
        document_path = requirement_document_path(job.id, settings.document_upload_dir)
        document_path.parent.mkdir(parents=True, exist_ok=True)
        document_path.write_bytes(document_content)

    store = _store(request)
    store.create(job)
    logger.info(
        "job %s created: ticket=%s source=%s repo=%s",
        job.id,
        ticket_key,
        requirement_source.value,
        repo_url,
    )

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


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    before: str | None = None,
) -> JobListResponse:
    user = require_current_user(request)
    # No owner filter (see every job) when auth is disabled (user is None) or
    # the requester is an admin; otherwise scoped to the requester's own jobs.
    owner_filter: str | None = None if user is None or user.role == UserRole.ADMIN else user.id
    jobs, next_cursor = _store(request).list_jobs(
        owner_user_id=owner_filter, limit=limit, before=before
    )
    return JobListResponse(
        jobs=[
            JobSummary(
                id=job.id,
                ticket_key=job.ticket_key,
                repo_url=job.repo_url,
                state=job.state,
                created_at=job.created_at,
                updated_at=job.updated_at,
                error_code=job.error.code.value if job.error else None,
            )
            for job in jobs
        ],
        next_cursor=next_cursor,
    )


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
    if job.repo_info is None or job.repo_info.source_kind not in (
        RepoSourceKind.LOCAL,
        RepoSourceKind.LOCAL_FOLDER,
        RepoSourceKind.REMOTE,
    ):
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


@router.post("/jobs/{job_id}/correct-validation", response_model=JobCreated, status_code=202)
async def correct_validation(job_id: str, request: Request) -> JobCreated:
    user = require_current_user(request)
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND)
    ensure_job_access(job, user)
    if (
        job.state != JobState.IMPLEMENTATION_READY
        or job.implementation_correction_attempted
        or not correctable_failures(job.validation_results)
    ):
        raise AppError(ErrorCode.VALIDATION_CORRECTION_NOT_AVAILABLE)
    if not job.workspace_path or not Path(job.workspace_path).exists():
        raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING)

    job.state = JobState.CORRECTING
    store.save(job)
    logger.info("job %s validation correction started", job.id)

    task = asyncio.create_task(
        run_validation_correction(job.id, store, get_settings(), _steps(request))
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JobCreated(job_id=job.id)


@router.post("/jobs/{job_id}/create-branch", response_model=BranchCreated)
async def create_branch(
    job_id: str, request: Request, payload: CreateBranchRequest | None = None
) -> BranchCreated:
    """Create a branch and commit the reviewed diff inside the isolated
    workspace. Synchronous (plain git plumbing, no LLM call, typically
    sub-second) — unlike implement/validate/correct this needs no background
    task or polling state. Never pushes anywhere; see CLAUDE.md's "Branch
    preparation" section.
    """
    user = require_current_user(request)
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND)
    ensure_job_access(job, user)
    if job.state != JobState.IMPLEMENTATION_READY or job.branch_name is not None:
        raise AppError(ErrorCode.BRANCH_CREATION_NOT_AVAILABLE)
    if not job.workspace_path or not Path(job.workspace_path).exists():
        raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING)

    branch_name = normalize_branch_name(payload.branch_name if payload is not None else None)
    commit_message = normalize_commit_message(
        payload.commit_message if payload is not None else None
    )

    result = await _steps(request).create_branch(
        job, Path(job.workspace_path), branch_name, commit_message
    )

    job.branch_name = result.branch_name
    job.branch_commit_sha = result.commit_sha
    job.branch_created_at = datetime.now(UTC)
    store.save(job)
    logger.info("job %s: branch %s created", job.id, result.branch_name)
    return BranchCreated(branch_name=result.branch_name, commit_sha=result.commit_sha)


_GITHUB_HTTPS_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_GITHUB_SSH_RE = re.compile(r"^(?:ssh://)?git@github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def github_compare_url(remote_url: str, branch: str) -> str | None:
    """A plain web link, not an API call — never opens or creates a PR
    itself, just gets the user one click away from doing it themselves."""
    match = _GITHUB_HTTPS_RE.match(remote_url) or _GITHUB_SSH_RE.match(remote_url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    return f"https://github.com/{owner}/{repo}/compare/{branch}?expand=1"


@router.post("/jobs/{job_id}/push-branch", response_model=BranchPushed)
async def push_branch(
    job_id: str, request: Request, payload: PushBranchRequest | None = None
) -> BranchPushed:
    """Push the job's already-created branch to the repo's real remote.
    Synchronous, like create-branch — plain git plumbing, no LLM call.
    Always uses ambient git auth (the same SSH-key/credential-helper model
    `git clone` already relies on); no per-user credential is read or
    injected. Never force-pushes; see CLAUDE.md's "Branch preparation"
    section and push_branch()'s own docstring in app/steps/branch_prep.py.
    """
    user = require_current_user(request)
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND)
    ensure_job_access(job, user)
    if job.branch_name is None:
        raise AppError(ErrorCode.BRANCH_PUSH_NOT_AVAILABLE)
    if not job.workspace_path or not Path(job.workspace_path).exists():
        raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING)

    branch_name = normalize_branch_name(payload.branch_name if payload is not None else None)

    # Idempotent: a repeat push with no rename requested, on a job already
    # pushed, re-reports the existing result instead of re-attempting —
    # otherwise it would incorrectly hit the "branch already exists on the
    # remote" collision check against its *own* prior push.
    if (
        job.branch_pushed_at is not None
        and job.branch_push_remote_url is not None
        and (branch_name is None or branch_name == job.branch_name)
    ):
        return BranchPushed(
            branch_name=job.branch_name,
            remote_url=job.branch_push_remote_url,
            compare_url=github_compare_url(job.branch_push_remote_url, job.branch_name),
        )

    result = await _steps(request).push_branch(job, Path(job.workspace_path), branch_name)

    job.branch_name = result.branch_name
    job.branch_pushed_at = datetime.now(UTC)
    job.branch_push_remote_url = result.remote_url
    store.save(job)
    logger.info("job %s: branch %s pushed", job.id, result.branch_name)
    return BranchPushed(
        branch_name=result.branch_name,
        remote_url=result.remote_url,
        compare_url=github_compare_url(result.remote_url, result.branch_name),
    )


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
            allow_non_git_folders=settings.allow_local_non_git_folders,
        ),
    )


@router.get("/admin/cost-summary", response_model=CostSummaryResponse)
async def get_cost_summary(request: Request) -> CostSummaryResponse:
    user = require_current_user(request)
    require_admin(user)
    store = _store(request)

    owners: list[OwnerCostSummary] = []
    for row in store.cost_summary_by_owner():
        resolved = store.get_user(row.owner_user_id) if row.owner_user_id else None
        owners.append(
            OwnerCostSummary(
                user_id=row.owner_user_id,
                email=resolved.email if resolved else None,
                display_name=resolved.display_name if resolved else None,
                job_count=row.job_count,
                planning_cost_usd=row.planning_cost_usd,
                implementation_cost_usd=row.implementation_cost_usd,
                total_cost_usd=row.planning_cost_usd + row.implementation_cost_usd,
            )
        )
    owners.sort(key=lambda owner: owner.total_cost_usd, reverse=True)

    return CostSummaryResponse(
        owners=owners,
        grand_total_usd=sum(owner.total_cost_usd for owner in owners),
    )
