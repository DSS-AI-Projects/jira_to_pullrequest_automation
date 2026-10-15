"""Hardened git invocation for the backend's own git runs on a job workspace
(security finding F1).

A job workspace (`var/workdir/<job>/repo`) is written by things the backend
doesn't trust: the implementation agent's Edit/Write tools, and the
validation step, which runs the repo's own tests/linters/`npm install`
scripts. Git runs repository-controlled commands from several places —
hooks (`.git/hooks/*`, or wherever `core.hooksPath` points), `core.fsmonitor`,
clean/smudge/process filters, diff textconv/external drivers, `gpg.program`,
`core.sshCommand`, credential helpers, `include.path` pulling in another
file, `url.*.insteadOf` silently redirecting a push elsewhere, ... Any of
those running during create-branch or push would run inside the backend with
its full environment, and during a delegated push a pre-push hook would see
the clicking user's auth header (git passes `-c` overrides to hooks via
GIT_CONFIG_PARAMETERS).

Two layers, for every backend git run on a workspace:

- `workspace_git_command()` puts `-c core.hooksPath=<an empty directory owned
  by this process>` and `-c core.fsmonitor=false` on the command line *after*
  any caller overrides, so no hook or fsmonitor in the repository can run
  whatever the repository's config says. Commits and pushes also pass
  `--no-verify` at the call site.
- `prepare_workspace_git()` runs before each git operation on a workspace
  and makes git's view of the repository trustworthy: `.git` must be a plain
  directory (not a symlink or a `gitdir:` file pointing at another
  repository, and without a `commondir` redirect), and `.git/config` is cut
  back to the keys `git clone`/`git init` themselves write — everything else
  (filters, diff drivers, includes, insteadOf, sshCommand, gpg, credential
  helpers, a husky-style `core.hooksPath`, ...) is dropped. An allowlist
  rather than a denylist, so a config key we didn't think of fails safe.

The machine's own global/system git config is trusted (it is the operator's,
like the ambient credential helper) and left alone. Agents are separately
denied any path inside `.git` (app/steps/workspace_guard.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

from app.core.logging import get_logger, redact

logger = get_logger(__name__)

# Keys `git clone` / `git init` (and the test fixtures' `git config user.*`)
# write into a repository's own config. Section and variable names come back
# lowercased from `git config --list`; subsection names keep their case.
_ALLOWED_CONFIG_KEY_RE = re.compile(
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|symlinks"
    r"|ignorecase|precomposeunicode)"
    r"|extensions\.(objectformat|refstorage)"
    r"|remote\.[^\n]+\.(url|fetch|tagopt)"
    r"|branch\.[^\n]+\.(remote|merge)"
    r"|user\.(name|email)"
)
_CONFIG_TIMEOUT_SECONDS = 30

_no_hooks_lock = threading.Lock()
_no_hooks_dir: Path | None = None


class UnsafeWorkspaceGitError(RuntimeError):
    """The workspace's `.git` was tampered with in a way that can't be
    repaired in place (it redirects git to another repository)."""


def _empty_hooks_dir() -> Path:
    """An empty directory for `core.hooksPath`, outside every workspace.
    Recreated if it has disappeared or something has been put in it."""
    global _no_hooks_dir
    with _no_hooks_lock:
        current = _no_hooks_dir
        try:
            usable = current is not None and current.is_dir() and not any(current.iterdir())
        except OSError:
            usable = False
        if not usable or current is None:
            current = Path(tempfile.mkdtemp(prefix="jira2pullreq-no-hooks-"))
            _no_hooks_dir = current
        return current


def hardening_config() -> list[str]:
    return [f"core.hooksPath={_empty_hooks_dir().as_posix()}", "core.fsmonitor=false"]


def workspace_git_command(workspace: Path, *args: str, config: Sequence[str] = ()) -> list[str]:
    """`git -c ... -C <workspace> <args>`. `config` entries become one-off
    `-c key=value` overrides; the hardening overrides come last so they win
    over anything a caller (or the repository) sets."""
    overrides = [part for entry in (*config, *hardening_config()) for part in ("-c", entry)]
    return ["git", *overrides, "-C", str(workspace), *args]


def _config_file_env() -> dict[str, str]:
    """`git config --file` reads or writes only the named file, so it needs
    nothing from the backend's environment beyond finding git itself — and
    gets nothing else (no server secrets; security invariant 6)."""
    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _read_config_entries(config_path: Path) -> list[tuple[str, str | None]]:
    # `--file` parses just that file: no include.* following (off by default
    # for --file), nothing executed. Run from the empty hooks dir so git
    # doesn't discover any repository from its cwd.
    completed = subprocess.run(
        [
            "git",
            *[part for entry in hardening_config() for part in ("-c", entry)],
            "config",
            "--file",
            str(config_path),
            "--null",
            "--list",
        ],
        cwd=_empty_hooks_dir(),
        env=_config_file_env(),
        capture_output=True,
        timeout=_CONFIG_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise UnsafeWorkspaceGitError("the workspace's .git/config could not be parsed")
    entries: list[tuple[str, str | None]] = []
    for raw in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if not raw:
            continue
        key, newline, value = raw.partition("\n")
        entries.append((key, value if newline else None))
    return entries


def _rewrite_config(config_path: Path, entries: list[tuple[str, str | None]]) -> None:
    replacement = config_path.with_name("config.jira2pullreq-rewrite")
    replacement.unlink(missing_ok=True)
    replacement.write_bytes(b"")
    for key, value in entries:
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(replacement),
                "--add",
                key,
                "true" if value is None else value,
            ],
            cwd=_empty_hooks_dir(),
            env=_config_file_env(),
            capture_output=True,
            timeout=_CONFIG_TIMEOUT_SECONDS,
            check=True,
        )
    replacement.replace(config_path)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def prepare_workspace_git(workspace: Path) -> None:
    """Make the workspace's git metadata safe for the backend to run git on.
    Call before each git operation on a job workspace (the agents and the
    validation step may have run since the last one)."""
    git_dir = workspace / ".git"
    if _is_link(git_dir) or not git_dir.is_dir():
        raise UnsafeWorkspaceGitError("the workspace's .git is not a plain directory")
    if (git_dir / "commondir").exists():
        raise UnsafeWorkspaceGitError("the workspace's .git redirects to another repository")

    config_path = git_dir / "config"
    if not config_path.exists():
        return
    try:
        entries = _read_config_entries(config_path)
        kept = [(key, value) for key, value in entries if _ALLOWED_CONFIG_KEY_RE.fullmatch(key)]
        if len(kept) == len(entries) and not _is_link(config_path):
            return
        dropped = sorted({key for key, _ in entries} - {key for key, _ in kept})
        logger.warning(
            "Dropped repository-controlled git config from workspace %s: %s",
            workspace,
            redact(", ".join(dropped)),
        )
        _rewrite_config(config_path, kept)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsafeWorkspaceGitError(
            f"the workspace's .git/config could not be sanitized: {type(exc).__name__}"
        ) from exc
