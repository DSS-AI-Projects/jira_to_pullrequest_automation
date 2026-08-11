"""Job step (b): clone the repo. Deterministic, no LLM.

Auth model: the subprocess inherits the machine's ambient git auth (SSH agent,
credential helper). The app never constructs, reads, or injects a credential;
GIT_TERMINAL_PROMPT=0 makes git fail fast instead of prompting, so a missing
credential becomes a typed CLONE_FAILED rather than a hang.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact
from app.jobs.models import RepoInfo, RepoSourceKind
from app.jobs.runner import CloneResult

logger = get_logger(__name__)
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

# Folder-copy safeguard (ALLOW_LOCAL_NON_GIT_FOLDERS): names never copied into
# the workspace, regardless of .gitignore. A plain folder has no git history
# to keep secrets out of a tracked tree, and the planning/implementation agent
# can Read/Grep anything that lands in the workspace (invariant 3) — so this
# list is a security control, not a convenience.
_FOLDER_COPY_ALWAYS_IGNORE = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    "target",
}
_ENV_FILE_RE = re.compile(r"^\.env(\..+)?$")
_GIT_INIT_IDENTITY = [
    "-c",
    "user.email=workspace@jira2pullreq.local",
    "-c",
    "user.name=jira2pullreq",
]


def _scrub_origin_url(url: str | None) -> str | None:
    """Strip any embedded userinfo (credentials) from a captured git remote
    URL before it is ever stored or returned to the client (invariant 4).

    For a LOCAL repo, `origin_url` is read from the user's own
    `git config --get remote.origin.url` — never validated at input time the
    way a submitted repo URL is, so it can legitimately contain a credential
    the user configured for their own convenience.
    """
    if not url:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.username and not parsed.password:
        return url
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


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


def _validate_local_source_path(path: Path) -> None:
    if not path.exists():
        raise AppError(ErrorCode.LOCAL_REPO_NOT_FOUND)
    if not path.is_dir():
        raise AppError(ErrorCode.LOCAL_REPO_NOT_DIRECTORY)


def _is_git_work_tree(path: Path, env: dict[str, str], timeout_seconds: int) -> bool:
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
    return inside.returncode == 0 and inside.stdout.strip() == "true"


def _inspect_repo(
    path: Path,
    source_kind: RepoSourceKind,
    env: dict[str, str],
    timeout_seconds: int,
    local_path: str | None = None,
) -> RepoInfo:
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path, env, timeout_seconds)
    commit_sha = _run_git(["rev-parse", "HEAD"], path, env, timeout_seconds)
    origin_url = _scrub_origin_url(
        _run_git_optional(["config", "--get", "remote.origin.url"], path, env, timeout_seconds)
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


def _load_gitignore_patterns(root: Path) -> list[str]:
    """Best-effort subset of .gitignore: skips comments/blank lines/negation,
    strips a leading '/' (root anchor) and trailing '/' (directory marker).
    Not full gitignore semantics (no '**', no per-directory scoping) — good
    enough to keep an author's own ignore rules out of the workspace copy."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [
        stripped.strip("/")
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith(("#", "!"))
    ]


def _build_copy_ignore(gitignore_patterns: list[str]):
    def _ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _FOLDER_COPY_ALWAYS_IGNORE
            or _ENV_FILE_RE.match(name)
            or any(fnmatch.fnmatch(name, pattern) for pattern in gitignore_patterns)
        }

    return _ignore


def _populate_folder_workspace(
    job_id: str, source: Path, dest: Path, timeout_seconds: int
) -> RepoInfo:
    """Copy a plain (non-git) folder into the isolated per-job workspace and
    git-init it there. Only the copy gets git history — the original folder
    is never touched. Everything downstream (diff capture, validation,
    implement) only ever inspects the workspace's own git state, so this is
    the only place that needs to know the source had no git history."""
    patterns = _load_gitignore_patterns(source)
    try:
        shutil.copytree(source, dest, ignore=_build_copy_ignore(patterns))
    except OSError as exc:
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"folder copy failed: {type(exc).__name__}: {exc}",
        ) from exc

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    _run_git(["init", "-q"], dest, env, timeout_seconds)
    _run_git(["add", "-A"], dest, env, timeout_seconds)
    _run_git(
        [
            *_GIT_INIT_IDENTITY,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"jira2pullreq: baseline snapshot for job {job_id}",
        ],
        dest,
        env,
        timeout_seconds,
    )
    commit_sha = _run_git(["rev-parse", "HEAD"], dest, env, timeout_seconds)
    return RepoInfo(
        source_kind=RepoSourceKind.LOCAL_FOLDER,
        branch=None,
        commit_sha=commit_sha,
        origin_url=None,
        is_dirty=False,
        local_path=str(source.resolve()),
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
        _validate_local_source_path(local_source)
        is_git = _is_git_work_tree(local_source, env, settings.clone_timeout_seconds)
        if not is_git and not settings.allow_local_non_git_folders:
            raise AppError(ErrorCode.LOCAL_REPO_NOT_GIT)
        if not is_git:
            logger.info("job %s: populating workspace from non-git folder %s", job_id, repo_url)
            repo_info = await asyncio.to_thread(
                _populate_folder_workspace,
                job_id,
                local_source,
                dest,
                settings.clone_timeout_seconds,
            )
            return CloneResult(clone_path=dest, repo_info=repo_info)
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
            source_info.branch or "", ticket_key
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
