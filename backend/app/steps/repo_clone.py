"""Job step (b): clone the repo. Deterministic, no LLM.

Auth model: the subprocess inherits the machine's ambient git auth (SSH agent,
credential helper). The app never constructs, reads, or injects a credential;
GIT_TERMINAL_PROMPT=0 makes git fail fast instead of prompting, so a missing
credential becomes a typed CLONE_FAILED rather than a hang.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact
from app.jobs.models import RepoInfo, RepoSourceKind
from app.jobs.runner import CloneResult

logger = get_logger(__name__)
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def build_clone_command(repo_url: str, dest: Path, *, local_source: bool = False) -> list[str]:
    """Pure command builder (tested): shallow, single-branch, no credentials."""
    command = [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
    ]
    if local_source:
        # Prevent local clones from using shared local clone optimizations.
        command.extend(["--no-local", "--no-hardlinks"])
    command.extend(["--", repo_url, str(dest)])
    return command


def _is_local_repo_path(repo_url: str) -> bool:
    return bool(_WINDOWS_ABS_PATH_RE.match(repo_url))


def _branch_matches_ticket(branch: str, ticket_key: str) -> bool:
    pattern = re.compile(rf"(^|[^A-Z0-9]){re.escape(ticket_key.upper())}($|[^A-Z0-9])")
    return bool(pattern.search(branch.upper()))


def _run_git(args: list[str], cwd: Path, env: dict[str, str], timeout_seconds: int) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"git metadata timed out after {timeout_seconds}s",
        ) from exc
    except OSError as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if completed.returncode != 0:
        detail = redact((completed.stderr or completed.stdout or "").strip())[:2000]
        raise AppError(ErrorCode.CLONE_FAILED, internal_detail=detail)
    return completed.stdout.strip()


def _run_git_optional(
    args: list[str], cwd: Path, env: dict[str, str], timeout_seconds: int
) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"git metadata timed out after {timeout_seconds}s",
        ) from exc
    except OSError as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _require_local_git_repo(path: Path, env: dict[str, str], timeout_seconds: int) -> None:
    if not path.exists():
        raise AppError(ErrorCode.LOCAL_REPO_NOT_FOUND)
    if not path.is_dir():
        raise AppError(ErrorCode.LOCAL_REPO_NOT_DIRECTORY)
    try:
        inside = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"git metadata timed out after {timeout_seconds}s",
        ) from exc
    except OSError as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise AppError(ErrorCode.LOCAL_REPO_NOT_GIT)


def _inspect_repo(
    path: Path,
    source_kind: RepoSourceKind,
    env: dict[str, str],
    timeout_seconds: int,
    local_path: str | None = None,
) -> RepoInfo:
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path, env, timeout_seconds)
    commit_sha = _run_git(["rev-parse", "HEAD"], path, env, timeout_seconds)
    origin_url = _run_git_optional(
        ["config", "--get", "remote.origin.url"], path, env, timeout_seconds
    )
    dirty_output = _run_git(["status", "--porcelain"], path, env, timeout_seconds)
    return RepoInfo(
        source_kind=source_kind,
        branch=branch,
        commit_sha=commit_sha,
        origin_url=origin_url or None,
        is_dirty=bool(dirty_output),
        local_path=local_path,
    )


async def clone_repo(job_id: str, ticket_key: str, repo_url: str, workdir: Path) -> CloneResult:
    settings = get_settings()
    dest = workdir / job_id / "repo"
    dest.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    source_info: RepoInfo | None = None
    is_local_source = _is_local_repo_path(repo_url)
    if is_local_source:
        local_source = Path(repo_url)
        _require_local_git_repo(local_source, env, settings.clone_timeout_seconds)
        source_info = _inspect_repo(
            local_source,
            RepoSourceKind.LOCAL,
            env,
            settings.clone_timeout_seconds,
            local_path=str(local_source.resolve()),
        )
        if source_info.is_dirty and not settings.allow_dirty_local_repos:
            raise AppError(ErrorCode.LOCAL_REPO_DIRTY)
        if settings.require_local_branch_ticket_match and not _branch_matches_ticket(
            source_info.branch, ticket_key
        ):
            raise AppError(
                ErrorCode.LOCAL_REPO_BRANCH_MISMATCH,
                internal_detail=f"branch={source_info.branch} ticket={ticket_key}",
            )

    command = build_clone_command(repo_url, dest, local_source=is_local_source)
    logger.info("job %s: cloning %s", job_id, repo_url)

    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            env=env,
            timeout=settings.clone_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"clone timed out after {settings.clone_timeout_seconds}s",
        ) from exc
    except OSError as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    if completed.returncode != 0:
        detail = redact((completed.stderr or completed.stdout or "").strip())[:2000]
        raise AppError(ErrorCode.CLONE_FAILED, internal_detail=detail)

    logger.info("job %s: clone complete", job_id)
    repo_info = source_info or _inspect_repo(
        dest,
        RepoSourceKind.REMOTE,
        env,
        settings.clone_timeout_seconds,
    )
    return CloneResult(clone_path=dest, repo_info=repo_info)
