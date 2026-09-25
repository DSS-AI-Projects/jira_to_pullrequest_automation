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
the workspace clone's own "origin" remote, and for the two credential modes
(delegated per-user OAuth token vs. ambient machine credentials —
PUSH_AUTH_MODE; see CLAUDE.md's "Branch preparation and push").
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact
from app.jobs.models import Job, RequirementSource
from app.jobs.runner import BranchResult, CommitAuthor, PushCredentials, PushResult

logger = get_logger(__name__)

_BRANCH_PREFIX = "jira2pullreq/"
_MAX_SLUG_LENGTH = 60
_MAX_BRANCH_NAME_LENGTH = 200
_MAX_COMMIT_MESSAGE_LENGTH = 2000
_GIT_TIMEOUT_SECONDS = 60
_PUSH_TIMEOUT_SECONDS = 120
# A git run carrying delegated credentials must never fall back to, or wait
# on, the machine's own credential prompts (Git Credential Manager on
# Windows, a terminal prompt anywhere): a rejected token is a typed error,
# not a push silently made as someone else or a hung request.
_NON_INTERACTIVE_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
_IDENTITY_UNSAFE_RE = re.compile(r"[<>\r\n\x00]")


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
    config: Sequence[str] = (),
) -> str:
    # `config` entries become one-off `-c key=value` overrides for this git
    # process only. They may carry a credential (a delegated push's auth
    # header), so they're deliberately left out of every error message below.
    overrides = [part for entry in config for part in ("-c", entry)]
    try:
        completed = subprocess.run(
            ["git", *overrides, "-C", str(workspace_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_NON_INTERACTIVE_ENV if config else None,
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


def _author_config(author: CommitAuthor | None) -> list[str]:
    """`user.name`/`user.email` overrides so the commit (author and
    committer) is attributed to the signed-in user rather than the
    workspace's placeholder `jira2pullreq` identity. Characters git's ident
    format can't hold (`<`, `>`, newlines) are dropped; an unusable value
    keeps the placeholder rather than failing the commit."""
    if author is None:
        return []
    name = _IDENTITY_UNSAFE_RE.sub("", author.name).strip()
    email = _IDENTITY_UNSAFE_RE.sub("", author.email).strip()
    if not name or "@" not in email:
        return []
    return [f"user.name={name}", f"user.email={email}"]


def _create_branch_sync(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
    commit_message: str | None,
    author: CommitAuthor | None = None,
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

    _run_git(workspace_path, "commit", "-m", message, config=_author_config(author))
    commit_sha = _run_git(workspace_path, "rev-parse", "HEAD")

    logger.info("job %s: created branch %s at %s", job.id, name, commit_sha)
    return BranchResult(branch_name=name, commit_sha=commit_sha)


async def create_branch(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
    commit_message: str | None,
    author: CommitAuthor | None = None,
) -> BranchResult:
    return await asyncio.to_thread(
        _create_branch_sync, job, workspace_path, branch_name, commit_message, author
    )


def _push_config(credentials: PushCredentials | None) -> list[str]:
    if credentials is None:
        return []
    # An empty `credential.helper` resets the helper list, so a rejected
    # token can never be retried with the machine's stored credentials.
    return ["credential.helper=", f"http.extraHeader={credentials.auth_header}"]


def _remote_branch_exists(
    workspace_path: Path, remote_url: str, name: str, credentials: PushCredentials | None
) -> bool:
    config = _push_config(credentials)
    completed = subprocess.run(
        [
            "git",
            *[part for entry in config for part in ("-c", entry)],
            "-C",
            str(workspace_path),
            "ls-remote",
            "--exit-code",
            "--heads",
            remote_url,
            name,
        ],
        capture_output=True,
        text=True,
        timeout=_PUSH_TIMEOUT_SECONDS,
        check=False,
        env=_NON_INTERACTIVE_ENV if config else None,
    )
    # Exit code 2 means "reachable but no matching ref" (--exit-code) — the
    # branch name is free. Any other non-zero code is a real problem
    # (unreachable remote, auth failure, ...), surfaced via the actual push
    # attempt below rather than guessed at here.
    return completed.returncode == 0


_AMBIENT_PUSH_FAILED_MESSAGE = (
    "Pushing the branch failed. Check that this machine's git credentials "
    "(SSH key / credential helper) can push to that remote."
)
_DELEGATED_PUSH_FAILED_MESSAGE = (
    "Pushing the branch failed. The repository host rejected the push — check "
    "that your connected account has write access to this repository (for a "
    "GitHub App, that the app is installed there with Contents: Read and "
    "write), and that the repository isn't read-only on the host's side."
)


def _push_branch_sync(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
    credentials: PushCredentials | None = None,
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
    if credentials is not None:
        # Same repository, by the https URL the user's token is valid for
        # (a LOCAL job's origin may be an SSH remote — see git_auth).
        remote_url = credentials.remote_url

    name = job.branch_name
    if branch_name and branch_name != job.branch_name:
        # Pushing under a different name (e.g. after a BRANCH_PUSH_REJECTED
        # collision) only changes the *remote* ref name below — the local
        # branch is never renamed. It used to be renamed first (`git branch
        # -m`), which, when the push then failed, left the workspace's branch
        # under the new name while the job still recorded the old one, so
        # every later attempt failed with "no branch named …" (surfaced as a
        # misleading BRANCH_NAME_INVALID) and the job was stuck.
        name = branch_name.strip()
        _validate_branch_name(workspace_path, name)

    if _remote_branch_exists(workspace_path, remote_url, name, credentials):
        raise AppError(ErrorCode.BRANCH_PUSH_REJECTED)

    # Push the job's recorded commit, not a local branch name, so the push
    # doesn't depend on what the workspace's branch happens to be called
    # (including a workspace left renamed by the old behavior above).
    source = job.branch_commit_sha or job.branch_name
    try:
        _run_git(
            workspace_path,
            "push",
            remote_url,
            f"{source}:refs/heads/{name}",
            error_code=ErrorCode.BRANCH_PUSH_FAILED,
            timeout=_PUSH_TIMEOUT_SECONDS,
            config=_push_config(credentials),
        )
    except AppError as exc:
        raise AppError(
            ErrorCode.BRANCH_PUSH_FAILED,
            user_message=(
                _AMBIENT_PUSH_FAILED_MESSAGE
                if credentials is None
                else _DELEGATED_PUSH_FAILED_MESSAGE
            ),
            internal_detail=exc.internal_detail,
        ) from exc

    logger.info("job %s: pushed branch %s to remote", job.id, name)
    return PushResult(branch_name=name, remote_url=remote_url)


async def push_branch(
    job: Job,
    workspace_path: Path,
    branch_name: str | None,
    credentials: PushCredentials | None = None,
) -> PushResult:
    """Push `job.branch_name` (or, if `branch_name` differs, rename it first)
    to the repo's real remote.

    With `credentials` (PUSH_AUTH_MODE=delegated), the push runs as the user
    who clicked Push, with their own OAuth token passed as a one-off
    `http.extraHeader` — never written into the workspace — and the
    machine's credential helper disabled, so a rejected token can't fall
    back to the machine's own identity. Without them (ambient mode), it uses
    the machine's own git auth (SSH key / credential helper), for local
    single-user development. See CLAUDE.md's "Branch preparation and push".
    """
    return await asyncio.to_thread(_push_branch_sync, job, workspace_path, branch_name, credentials)
