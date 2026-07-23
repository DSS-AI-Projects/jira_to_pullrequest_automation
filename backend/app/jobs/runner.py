"""Async job runner: drives a job through its steps to a terminal state.

Failure contract: an AppError from a step becomes a typed JobError on the
record; anything else is logged (redacted) and becomes a generic INTERNAL
error. A job can never end outside PLAN_READY or FAILED.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DEFAULT_MESSAGES, AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import (
    AgentUsage,
    ImplementationDiff,
    ImplementationDiffFile,
    ImplementationResult,
    Job,
    JobError,
    JobState,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
)
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
class ImplementationStepResult:
    result: ImplementationResult
    usage: AgentUsage


@dataclass(frozen=True)
class CloneResult:
    clone_path: Path
    repo_info: RepoInfo


@dataclass(frozen=True)
class JobSteps:
    """The four job steps, injectable for tests."""

    fetch_ticket: Callable[[Job, JobStore], Awaitable[TicketData]]
    clone_repo: Callable[[str, str, str, Path], Awaitable[CloneResult]]
    build_repo_map: Callable[[Path], Awaitable[RepoMap]]
    generate_plan: Callable[[TicketData, RepoMap, Path], Awaitable[PlanResult]]
    implement_plan: Callable[[Job, Path], Awaitable[ImplementationStepResult]]
    validate_workspace: Callable[[Path], Awaitable[list[ValidationResult]]]


def _advance(store: JobStore, job: Job, state: JobState) -> Job:
    job.state = state
    return store.save(job)


def _fail(
    store: JobStore,
    job: Job,
    code: ErrorCode,
    message: str,
    *,
    failure_state: JobState = JobState.FAILED,
) -> None:
    job.error = JobError(code=code, message=message, stage=job.state)
    job.state = failure_state
    store.save(job)


def _run_git_command(workspace_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace_path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout


def _read_git_head_sync(workspace_path: Path) -> str | None:
    try:
        return _run_git_command(workspace_path, "rev-parse", "HEAD").strip() or None
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        return None


def _parse_numstat(output: str) -> tuple[int | None, int | None, bool]:
    line = output.strip()
    if not line:
        return None, None, False

    additions_text, deletions_text, *_rest = line.split("\t", maxsplit=2)
    is_binary = additions_text == "-" or deletions_text == "-"
    additions = None if additions_text == "-" else int(additions_text)
    deletions = None if deletions_text == "-" else int(deletions_text)
    return additions, deletions, is_binary


def _collect_implementation_diff_sync(workspace_path: Path, base_ref: str) -> ImplementationDiff:
    overall_patch = _run_git_command(
        workspace_path,
        "diff",
        "--no-ext-diff",
        "--find-renames",
        "--unified=3",
        base_ref,
        "--",
    )
    changed_paths = [
        line
        for line in _run_git_command(
            workspace_path, "diff", "--name-only", base_ref, "--"
        ).splitlines()
        if line.strip()
    ]
    files: list[ImplementationDiffFile] = []
    for path in changed_paths:
        patch = _run_git_command(
            workspace_path,
            "diff",
            "--no-ext-diff",
            "--find-renames",
            "--unified=3",
            base_ref,
            "--",
            path,
        )
        additions, deletions, is_binary = _parse_numstat(
            _run_git_command(workspace_path, "diff", "--numstat", base_ref, "--", path)
        )
        files.append(
            ImplementationDiffFile(
                path=path,
                patch=patch,
                additions=additions,
                deletions=deletions,
                is_binary=is_binary,
            )
        )
    return ImplementationDiff(overall_patch=overall_patch, files=files)


async def _collect_implementation_diff(
    workspace_path: Path, base_ref: str
) -> ImplementationDiff | None:
    try:
        return await asyncio.to_thread(_collect_implementation_diff_sync, workspace_path, base_ref)
    except subprocess.CalledProcessError:
        logger.warning(
            "failed to collect implementation diff for %s",
            workspace_path,
            exc_info=True,
        )
    except FileNotFoundError:
        logger.warning(
            "git is unavailable while collecting implementation diff for %s",
            workspace_path,
        )
    return None


async def run_job(job_id: str, store: JobStore, settings: Settings, steps: JobSteps) -> None:
    job = store.get(job_id)
    if job is None:  # pragma: no cover - defensive
        logger.error("run_job: job %s not found", job_id)
        return
    try:
        job = _advance(store, job, JobState.FETCHING_TICKET)
        ticket = await steps.fetch_ticket(job, store)

        job = _advance(store, job, JobState.CLONING_REPO)
        clone_result = await steps.clone_repo(
            job.id, job.ticket_key, job.repo_url, settings.workdir
        )
        clone_path = clone_result.clone_path
        job.repo_info = clone_result.repo_info
        job.workspace_path = str(clone_path)

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


async def run_implementation(
    job_id: str, store: JobStore, settings: Settings, steps: JobSteps
) -> None:
    del settings  # reserved for future implementation-step configuration
    job = store.get(job_id)
    if job is None:  # pragma: no cover - defensive
        logger.error("run_implementation: job %s not found", job_id)
        return
    try:
        if job.state != JobState.IMPLEMENTATION_QUEUED or job.plan is None:
            raise AppError(ErrorCode.IMPLEMENTATION_NOT_READY)
        if job.repo_info is None or job.repo_info.source_kind != RepoSourceKind.LOCAL:
            raise AppError(ErrorCode.IMPLEMENTATION_NOT_SUPPORTED)
        if not job.workspace_path:
            raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING)

        workspace_path = Path(job.workspace_path)
        if not workspace_path.exists():
            raise AppError(
                ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING,
                internal_detail=f"workspace path missing: {workspace_path}",
            )

        base_ref = await asyncio.to_thread(_read_git_head_sync, workspace_path)
        base_ref = base_ref or "HEAD"
        job.implementation_started_at = store.save(job).updated_at
        job = _advance(store, job, JobState.IMPLEMENTING)
        implementation = await steps.implement_plan(job, workspace_path)

        job.implementation_result = implementation.result
        job.implementation_diff = await _collect_implementation_diff(workspace_path, base_ref)
        job.implementation_usage = implementation.usage
        store.save(job)
        job = _advance(store, job, JobState.VALIDATING)
        job.validation_results = await steps.validate_workspace(workspace_path)
        job.implementation_finished_at = store.save(job).updated_at
        job.state = JobState.IMPLEMENTATION_READY
        store.save(job)
        logger.info("job %s: implementation ready", job.id)
    except AppError as err:
        logger.warning(
            "job %s implementation failed at %s: %s (%s)",
            job.id,
            job.state,
            err.code,
            err.internal_detail or "no detail",
        )
        _fail(
            store,
            job,
            err.code,
            err.user_message,
            failure_state=JobState.IMPLEMENTATION_FAILED,
        )
    except Exception:
        logger.exception("job %s implementation crashed at %s", job.id, job.state)
        _fail(
            store,
            job,
            ErrorCode.INTERNAL,
            DEFAULT_MESSAGES[ErrorCode.INTERNAL],
            failure_state=JobState.IMPLEMENTATION_FAILED,
        )
    finally:
        final = store.get(job.id)
        if final is not None and not final.is_terminal:  # pragma: no cover - defensive
            _fail(
                store,
                final,
                ErrorCode.INTERNAL,
                DEFAULT_MESSAGES[ErrorCode.INTERNAL],
                failure_state=JobState.IMPLEMENTATION_FAILED,
            )
