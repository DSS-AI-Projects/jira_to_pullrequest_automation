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
class BranchResult:
    branch_name: str
    commit_sha: str


@dataclass(frozen=True)
class PushResult:
    branch_name: str
    remote_url: str


@dataclass(frozen=True)
class JobSteps:
    """The job steps, injectable for tests."""

    fetch_ticket: Callable[[Job, JobStore], Awaitable[TicketData]]
    clone_repo: Callable[[str, str, str, Path], Awaitable[CloneResult]]
    build_repo_map: Callable[[Path], Awaitable[RepoMap]]
    generate_plan: Callable[[TicketData, RepoMap, Path, str | None], Awaitable[PlanResult]]
    implement_plan: Callable[
        [Job, Path, list[ValidationResult] | None], Awaitable[ImplementationStepResult]
    ]
    validate_workspace: Callable[[Path], Awaitable[list[ValidationResult]]]
    create_branch: Callable[[Job, Path, str | None, str | None], Awaitable[BranchResult]]
    push_branch: Callable[[Job, Path, str | None], Awaitable[PushResult]]


def _advance(store: JobStore, job: Job, state: JobState) -> Job:
    job.state = state
    return store.save(job)


_ACTIVITY_LOG_CAP = 50


def _start_activity_log(store: JobStore, job: Job) -> Callable[[str], None]:
    """Clear the job's activity log for a fresh agent-backed step, and
    return a sink that appends+persists each reported progress line.

    The sink is a plain synchronous function: it runs inside the agent's
    background thread (see app/steps/agent_progress.py's module docstring),
    not the main event loop, so it must not be a coroutine. JobStore.save is
    already thread-safe (sqlite3 connection opened with
    check_same_thread=False, guarded by its own lock), so calling it
    directly from that thread is safe.
    """
    job.activity_log = []
    store.save(job)

    def sink(line: str) -> None:
        job.activity_log = [*job.activity_log, line][-_ACTIVITY_LOG_CAP:]
        store.save(job)

    return sink


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
    # Stage everything first: `git diff` never shows untracked files on its
    # own, and the implement agent has no Bash tool to run `git add` itself —
    # without this, newly created files would silently be missing from the
    # diff even though the agent reported creating them.
    _run_git_command(workspace_path, "add", "-A")

    overall_patch = _run_git_command(
        workspace_path,
        "diff",
        "--cached",
        "--no-ext-diff",
        "--find-renames",
        "--unified=3",
        base_ref,
        "--",
    )
    changed_paths = [
        line
        for line in _run_git_command(
            workspace_path, "diff", "--cached", "--name-only", base_ref, "--"
        ).splitlines()
        if line.strip()
    ]
    files: list[ImplementationDiffFile] = []
    for path in changed_paths:
        patch = _run_git_command(
            workspace_path,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--find-renames",
            "--unified=3",
            base_ref,
            "--",
            path,
        )
        additions, deletions, is_binary = _parse_numstat(
            _run_git_command(workspace_path, "diff", "--cached", "--numstat", base_ref, "--", path)
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
        # Deferred import: app.steps.__init__ imports JobSteps from this
        # module, so a top-level import here would be circular.
        from app.steps.agent_progress import report_progress_to

        with report_progress_to(_start_activity_log(store, job)):
            result = await steps.generate_plan(ticket, repo_map, clone_path, job.planning_notes)

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
        if err.usage is not None:
            job.usage = AgentUsage.model_validate(err.usage)
        _fail(store, job, err.code, err.user_message)
    except Exception:
        logger.exception("job %s crashed at %s", job.id, job.state)
        _fail(store, job, ErrorCode.INTERNAL, DEFAULT_MESSAGES[ErrorCode.INTERNAL])
    finally:
        final = store.get(job.id)
        if final is not None and not final.is_terminal:  # pragma: no cover - defensive
            _fail(store, final, ErrorCode.INTERNAL, DEFAULT_MESSAGES[ErrorCode.INTERNAL])


async def _refresh_diff_best_effort(job: Job) -> None:
    """Best-effort: (re-)collect the diff between the workspace's current
    state and the recorded baseline, so a developer sees whatever the agent
    actually wrote to disk even though the run is about to be marked
    failed — turns/budget spent on edits that landed before a later tool
    call, or the agent itself, failed shouldn't be invisible.

    Used both when implementation fails outright (`implementation_diff` is
    still unset at that point — the normal diff-collection line is only
    reached on the success path) and when a validation-correction attempt
    fails (`implementation_diff` already holds the *original* successful
    implementation's diff; a correction only ever adds uncommitted edits on
    top of that same baseline, so re-collecting can only add information,
    never lose the original). Only overwrites when a non-empty diff is
    actually found — never regresses a job to a worse or missing diff.
    """
    if not job.workspace_path or not job.implementation_baseline_commit_sha:
        return
    workspace_path = Path(job.workspace_path)
    if not workspace_path.exists():
        return
    diff = await _collect_implementation_diff(
        workspace_path, job.implementation_baseline_commit_sha
    )
    if diff is not None and diff.files:
        job.implementation_diff = diff


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
        if job.repo_info is None or job.repo_info.source_kind not in (
            RepoSourceKind.LOCAL,
            RepoSourceKind.LOCAL_FOLDER,
            RepoSourceKind.REMOTE,
        ):
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
        job.implementation_baseline_commit_sha = base_ref
        job.implementation_started_at = store.save(job).updated_at
        job = _advance(store, job, JobState.IMPLEMENTING)
        # Deferred import: app.steps.__init__ imports JobSteps from this
        # module, so a top-level import here would be circular.
        from app.steps.agent_progress import report_progress_to

        with report_progress_to(_start_activity_log(store, job)):
            implementation = await steps.implement_plan(job, workspace_path, None)
        # Recorded as soon as the agent call returns, before the consistency
        # check below can raise — so even the "claimed changes never landed"
        # failure path still reports what the (completed, cost-incurring)
        # agent run actually used.
        job.implementation_usage = implementation.usage
        store.save(job)
        diff = await _collect_implementation_diff(workspace_path, base_ref)

        if implementation.result.changed_files and (diff is None or not diff.files):
            # The agent's own report doesn't match reality — its structured
            # output claimed file changes that never actually landed on disk
            # (most likely a tool call that silently failed and was not
            # recovered from). Surfacing this as a hollow "success" with an
            # empty diff would be misleading; fail explicitly instead.
            claimed_paths = ", ".join(change.path for change in implementation.result.changed_files)
            raise AppError(
                ErrorCode.IMPLEMENTATION_INVALID,
                user_message=(
                    "The implementation agent reported changing "
                    f"{len(implementation.result.changed_files)} file(s) "
                    f"({claimed_paths}), but no actual code differences were found in "
                    "the workspace afterward — most likely one of its edit attempts "
                    "failed silently partway through and it did not recover. This is "
                    "typically a one-off execution glitch, not a problem with the "
                    "ticket itself — retrying often succeeds without any changes "
                    "needed."
                ),
                internal_detail=(
                    f"agent reported {len(implementation.result.changed_files)} changed "
                    "file(s) but no working-tree differences were detected against the "
                    "pre-implementation baseline"
                ),
            )

        job.implementation_result = implementation.result
        job.implementation_diff = diff
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
        # err.usage is only set when implement_plan itself raised after an
        # agent call completed (e.g. IMPLEMENTATION_INVALID, BUDGET_EXCEEDED).
        # When implement_plan succeeded and a later step in this function
        # raised instead (e.g. the "claimed changes never landed" consistency
        # check), job.implementation_usage was already set above right after
        # that call returned — never overwrite it with an absent err.usage.
        if err.usage is not None:
            job.implementation_usage = AgentUsage.model_validate(err.usage)
        await _refresh_diff_best_effort(job)
        _fail(
            store,
            job,
            err.code,
            err.user_message,
            failure_state=JobState.IMPLEMENTATION_FAILED,
        )
    except Exception:
        logger.exception("job %s implementation crashed at %s", job.id, job.state)
        await _refresh_diff_best_effort(job)
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


def _record_correction_failure(store: JobStore, job: Job, code: ErrorCode, message: str) -> None:
    # A correction attempt is strictly best-effort: whether it succeeds,
    # partially succeeds, or the agent call itself fails, the job always
    # lands back on IMPLEMENTATION_READY. The original implementation_result
    # and implementation_diff are left untouched — a failed correction never
    # regresses a job that already had a working (if imperfectly validated)
    # implementation.
    job.implementation_correction_error = JobError(code=code, message=message, stage=job.state)
    job.implementation_correction_attempted = True
    job.state = JobState.IMPLEMENTATION_READY
    store.save(job)


async def run_validation_correction(
    job_id: str, store: JobStore, settings: Settings, steps: JobSteps
) -> None:
    # Deferred import: app.steps.__init__ imports JobSteps from this module,
    # so a top-level import here would be circular.
    from app.steps.validation_runner import correctable_failures

    del settings  # reserved for future correction-step configuration
    job = store.get(job_id)
    if job is None:  # pragma: no cover - defensive
        logger.error("run_validation_correction: job %s not found", job_id)
        return
    try:
        if job.state != JobState.CORRECTING:
            raise AppError(ErrorCode.VALIDATION_CORRECTION_NOT_AVAILABLE)
        if not job.workspace_path:
            raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING)

        workspace_path = Path(job.workspace_path)
        if not workspace_path.exists():
            raise AppError(
                ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING,
                internal_detail=f"workspace path missing: {workspace_path}",
            )

        failed_results = correctable_failures(job.validation_results)
        base_ref = job.implementation_baseline_commit_sha
        if not base_ref:
            # Defensive fallback for a job implemented before this field
            # existed on Job.
            base_ref = await asyncio.to_thread(_read_git_head_sync, workspace_path)
            base_ref = base_ref or "HEAD"

        # Deferred import: app.steps.__init__ imports JobSteps from this
        # module, so a top-level import here would be circular.
        from app.steps.agent_progress import report_progress_to

        with report_progress_to(_start_activity_log(store, job)):
            correction = await steps.implement_plan(job, workspace_path, failed_results)
        job.implementation_correction_result = correction.result
        job.implementation_correction_usage = correction.usage
        store.save(job)

        job = _advance(store, job, JobState.REVALIDATING)
        diff = await _collect_implementation_diff(workspace_path, base_ref)
        if diff is not None:
            # A diff-collection failure here is best-effort like elsewhere in
            # this pipeline — keep whatever diff the original implementation
            # already recorded rather than clobbering it with None.
            job.implementation_diff = diff
        job.validation_results = await steps.validate_workspace(workspace_path)
        job.implementation_correction_attempted = True
        job.state = JobState.IMPLEMENTATION_READY
        store.save(job)
        logger.info("job %s: validation correction complete", job.id)
    except AppError as err:
        logger.warning(
            "job %s validation correction failed at %s: %s (%s)",
            job.id,
            job.state,
            err.code,
            err.internal_detail or "no detail",
        )
        if err.usage is not None:
            job.implementation_correction_usage = AgentUsage.model_validate(err.usage)
        await _refresh_diff_best_effort(job)
        _record_correction_failure(store, job, err.code, err.user_message)
    except Exception:
        logger.exception("job %s validation correction crashed at %s", job.id, job.state)
        await _refresh_diff_best_effort(job)
        _record_correction_failure(
            store, job, ErrorCode.INTERNAL, DEFAULT_MESSAGES[ErrorCode.INTERNAL]
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
