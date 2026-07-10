"""Async job runner: drives a job through its steps to a terminal state.

Failure contract: an AppError from a step becomes a typed JobError on the
record; anything else is logged (redacted) and becomes a generic INTERNAL
error. A job can never end outside PLAN_READY or FAILED.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DEFAULT_MESSAGES, AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import AgentUsage, Job, JobError, JobState
from app.jobs.store import JobStore
from app.schemas.plan import Plan
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData

logger = get_logger(__name__)


@dataclass(frozen=True)
class PlanResult:
    plan: Plan
    usage: AgentUsage


@dataclass(frozen=True)
class JobSteps:
    """The four job steps, injectable for tests."""

    fetch_ticket: Callable[[str], Awaitable[TicketData]]
    clone_repo: Callable[[str, str, Path], Awaitable[Path]]  # (job_id, repo_url, workdir)
    build_repo_map: Callable[[Path], Awaitable[RepoMap]]
    generate_plan: Callable[[TicketData, RepoMap, Path], Awaitable[PlanResult]]


def _advance(store: JobStore, job: Job, state: JobState) -> Job:
    job.state = state
    return store.save(job)


def _fail(store: JobStore, job: Job, code: ErrorCode, message: str) -> None:
    job.error = JobError(code=code, message=message, stage=job.state)
    job.state = JobState.FAILED
    store.save(job)


async def run_job(job_id: str, store: JobStore, settings: Settings, steps: JobSteps) -> None:
    job = store.get(job_id)
    if job is None:  # pragma: no cover - defensive
        logger.error("run_job: job %s not found", job_id)
        return
    try:
        job = _advance(store, job, JobState.FETCHING_TICKET)
        ticket = await steps.fetch_ticket(job.ticket_key)

        job = _advance(store, job, JobState.CLONING_REPO)
        clone_path = await steps.clone_repo(job.id, job.repo_url, settings.workdir)

        job = _advance(store, job, JobState.MAPPING_REPO)
        repo_map = await steps.build_repo_map(clone_path)

        job = _advance(store, job, JobState.PLANNING)
        result = await steps.generate_plan(ticket, repo_map, clone_path)

        job.plan = result.plan
        job.usage = result.usage
        job.state = JobState.PLAN_READY
        store.save(job)
        logger.info("job %s: plan ready", job.id)
    except AppError as err:
        logger.warning(
            "job %s failed at %s: %s (%s)",
            job.id,
            job.state,
            err.code,
            err.internal_detail or "no detail",
        )
        _fail(store, job, err.code, err.user_message)
    except Exception:
        logger.exception("job %s crashed at %s", job.id, job.state)
        _fail(store, job, ErrorCode.INTERNAL, DEFAULT_MESSAGES[ErrorCode.INTERNAL])
    finally:
        final = store.get(job.id)
        if final is not None and not final.is_terminal:  # pragma: no cover - defensive
            _fail(store, final, ErrorCode.INTERNAL, DEFAULT_MESSAGES[ErrorCode.INTERNAL])
