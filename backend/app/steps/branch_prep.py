"""Job steps: create a branch + commit the reviewed implementation diff
inside the isolated workspace, and (separately, later) push that branch to
the repo's real remote. Deterministic, no LLM — plain git plumbing, matching
the diff-collection code in app/jobs/runner.py.

`create_branch` never pushes anywhere and never touches the user's original
repo — only the isolated workspace clone. Capped at one successful call per
job (enforced by the caller, app/api/routes.py, via `Job.branch_name`):
unlike validation correction there's no LLM budget to protect, but retrying
after a *successful* commit would mean re-pointing an existing branch at a
fresh checkout of the baseline, which discards the working tree the first
commit already absorbed — simplest and safest is to get it right once. A
validation failure (bad name, nothing to commit) never touches git state, so
it's always safely retryable with corrected input.

`push_branch` is a separate, later, explicitly-triggered step — see its own
docstring below for why it targets `Job.repo_info.origin_url` rather than
the workspace clone's own "origin" remote, and for the ambient-git-auth
credential model (Phase A: no per-user token is ever handed to a git
subprocess — see CLAUDE.md's "Branch preparation" section).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact
from app.jobs.models import Job, RequirementSource
from app.jobs.runner import BranchResult, PushResult

logger = get_logger(__name__)

_BRANCH_PREFIX = "jira2pullreq/"
_MAX_SLUG_LENGTH = 60
_MAX_BRANCH_NAME_LENGTH = 200
_MAX_COMMIT_MESSAGE_LENGTH = 2000
_GIT_TIMEOUT_SECONDS = 60
_PUSH_TIMEOUT_SECONDS = 120


def _slugify(text: str, max_length: int = _MAX_SLUG_LENGTH) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].strip("-")


def default_branch_name(job: Job) -> str:
    """A ticket-based branch name, or a slugified-summary name for a
    document-sourced job whose synthetic `DOC-XXXXXXXX` key alone wouldn't
    mean anything to a reviewer."""
    if job.requirement_source == RequirementSource.DOCUMENT:
        summary = job.plan.summary if job.plan else ""
        slug = _slugify(summary)
        suffix = f"-{slug}" if slug else ""
        return f"{_BRANCH_PREFIX}{job.ticket_key.lower()}{suffix}"
    return f"{_BRANCH_PREFIX}{job.ticket_key}"


def default_commit_message(job: Job) -> str:
    summary = job.plan.summary if job.plan else "Implement changes"
    return f"{job.ticket_key}: {summary}"


def _run_git(
    workspace_path: Path,
    *args: str,
    error_code: ErrorCode = ErrorCode.BRANCH_CREATION_FAILED,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError(
            error_code,
            internal_detail=redact(
                f"git {' '.join(args)} failed to launch: {type(exc).__name__}: {exc}"
            ),
        ) from exc
    if completed.returncode != 0:
        detail = redact((completed.stderr or completed.stdout or "").strip())[:2000]
        raise AppError(error_code, internal_detail=detail)
    return completed.stdout.strip()


def _validate_branch_name(workspace_path: Path, name: str) -> None:
    if not name or len(name) > _MAX_BRANCH_NAME_LENGTH:
        raise AppError(ErrorCode.BRANCH_NAME_INVALID)
    # Delegate to git's own ref-name grammar rather than reimplementing it
    # (no double dots, no "@{", no trailing ".lock", control characters,
    # etc. — git already gets this right).
    completed = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise AppError(ErrorCode.BRANCH_NAME_INVALID)


def _create_branch_sync(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
    commit_message: str | None,
) -> BranchResult:
    name = (branch_name or default_branch_name(job)).strip()
    message = (commit_message or default_commit_message(job)).strip()
    if len(message) > _MAX_COMMIT_MESSAGE_LENGTH:
        raise AppError(ErrorCode.INPUT_INVALID)

    _validate_branch_name(workspace_path, name)

    # HEAD is still at the baseline at this point — the implement/correction
    # steps only ever `git add`, never `git commit` (see
    # _collect_implementation_diff in app/jobs/runner.py) — so this is a
    # no-op move that doesn't touch the working tree, not a real checkout.
    baseline = job.implementation_baseline_commit_sha or _run_git(
        workspace_path, "rev-parse", "HEAD"
    )
    _run_git(workspace_path, "checkout", "-B", name, baseline)
    _run_git(workspace_path, "add", "-A")

    staged = _run_git(workspace_path, "diff", "--cached", "--name-only")
    if not staged.strip():
        raise AppError(
            ErrorCode.BRANCH_CREATION_FAILED,
            user_message="There are no changes to commit for this job.",
        )

    _run_git(workspace_path, "commit", "-m", message)
    commit_sha = _run_git(workspace_path, "rev-parse", "HEAD")

    logger.info("job %s: created branch %s at %s", job.id, name, commit_sha)
    return BranchResult(branch_name=name, commit_sha=commit_sha)


async def create_branch(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
    commit_message: str | None,
) -> BranchResult:
    return await asyncio.to_thread(
        _create_branch_sync, job, workspace_path, branch_name, commit_message
    )


def _remote_branch_exists(workspace_path: Path, remote_url: str, name: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(workspace_path), "ls-remote", "--exit-code", "--heads", remote_url, name],
        capture_output=True,
        text=True,
        timeout=_PUSH_TIMEOUT_SECONDS,
        check=False,
    )
    # Exit code 2 means "reachable but no matching ref" (--exit-code) — the
    # branch name is free. Any other non-zero code is a real problem
    # (unreachable remote, auth failure, ...), surfaced via the actual push
    # attempt below rather than guessed at here.
    return completed.returncode == 0


def _push_branch_sync(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
) -> PushResult:
    if job.branch_name is None:
        raise AppError(ErrorCode.BRANCH_PUSH_NOT_AVAILABLE)

    # The workspace clone's own "origin" remote is not necessarily the real
    # upstream: for a LOCAL job it points at the user's own local source path
    # (that's literally what it was cloned from), not wherever that source's
    # own repo is actually hosted. `Job.repo_info.origin_url` was captured
    # from the *original* source's own git config before cloning (see
    # repo_clone.py's _inspect_repo call on the local source), so it's the
    # correct real-remote target for both LOCAL and REMOTE jobs alike — and
    # is None exactly when there's genuinely nothing to push to (a plain
    # LOCAL_FOLDER source, or a LOCAL repo with no configured origin).
    remote_url = job.repo_info.origin_url if job.repo_info else None
    if not remote_url:
        raise AppError(
            ErrorCode.BRANCH_PUSH_NOT_AVAILABLE,
            user_message="This job's repository has no remote to push to.",
        )

    name = job.branch_name
    if branch_name and branch_name != job.branch_name:
        # A rename-and-retry: safe to do locally since the branch is already
        # committed (nothing uncommitted to lose) — this is just relabeling
        # the same commit, not a fresh checkout from baseline.
        new_name = branch_name.strip()
        _validate_branch_name(workspace_path, new_name)
        _run_git(
            workspace_path,
            "branch",
            "-m",
            job.branch_name,
            new_name,
            error_code=ErrorCode.BRANCH_NAME_INVALID,
        )
        name = new_name

    if _remote_branch_exists(workspace_path, remote_url, name):
        raise AppError(ErrorCode.BRANCH_PUSH_REJECTED)

    _run_git(
        workspace_path,
        "push",
        remote_url,
        f"{name}:refs/heads/{name}",
        error_code=ErrorCode.BRANCH_PUSH_FAILED,
        timeout=_PUSH_TIMEOUT_SECONDS,
    )

    logger.info("job %s: pushed branch %s to remote", job.id, name)
    return PushResult(branch_name=name, remote_url=remote_url)


async def push_branch(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
) -> PushResult:
    """Push `job.branch_name` (or, if `branch_name` differs, rename it first)
    to the repo's real remote, using only ambient git auth — the same
    SSH-key/credential-helper model `git clone` already relies on. No
    per-user credential is ever read or injected here (see the module
    docstring and CLAUDE.md's "Branch preparation" section); a shared
    multi-user deployment where individual developers don't have their own
    push access on the server machine needs a separate, later capability.
    """
    return await asyncio.to_thread(_push_branch_sync, job, workspace_path, branch_name)
