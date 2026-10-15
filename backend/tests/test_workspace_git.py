"""Security finding F1: repository-controlled execution during the backend's
own git runs on a job workspace.

The implementation agent and the validation step (which runs the repo's own
tests/linters) can both write into a workspace. If anything they wrote into
`.git` — a hook, `core.hooksPath`, `core.fsmonitor`, a filter driver — ran
during create-branch or push, it would run inside the backend with the
server's full environment, and a pre-push hook would see the delegated auth
header of whoever clicked Push (git hands `-c` overrides to hooks via
GIT_CONFIG_PARAMETERS). Every planted trigger below writes a marker file if
it runs; none may run.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from app.core import workspace_git
from app.jobs import runner
from app.jobs.models import RepoInfo, RepoSourceKind
from app.jobs.runner import PushCredentials
from app.steps import branch_prep
from tests import test_branch_prep

_collect_implementation_diff_sync = runner._collect_implementation_diff_sync  # pyright: ignore[reportPrivateUsage]
_init_bare_remote = test_branch_prep._init_bare_remote  # pyright: ignore[reportPrivateUsage]
_init_git_repo = test_branch_prep._init_git_repo  # pyright: ignore[reportPrivateUsage]
_job = test_branch_prep._job  # pyright: ignore[reportPrivateUsage]
_remote_ref_sha = test_branch_prep._remote_ref_sha  # pyright: ignore[reportPrivateUsage]


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _marker_script(marker: Path, label: str) -> str:
    # POSIX sh: git runs hooks (and fsmonitor/filter commands) through sh on
    # Linux and through Git for Windows' bundled sh on Windows.
    return f'#!/bin/sh\necho {label} >> "{marker.as_posix()}"\ncat >/dev/null 2>&1 || true\n'


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _plant_everything(workspace: Path, marker: Path) -> None:
    """Every repository-controlled way to make the backend's git run a
    command: default hooks dir, a `core.hooksPath` dir, `core.fsmonitor`, and
    a clean/smudge filter + diff textconv + external diff selected by a
    `.gitattributes` the agent could legitimately write."""
    hooks = workspace / ".git" / "hooks"
    planted = workspace / "planted-hooks"
    for directory in (hooks, planted):
        for hook in ("pre-commit", "commit-msg", "post-commit", "pre-push", "post-checkout"):
            _write_executable(directory / hook, _marker_script(marker, f"{directory.name}-{hook}"))
    fsmonitor = workspace / "fsmonitor.sh"
    _write_executable(fsmonitor, _marker_script(marker, "fsmonitor"))
    filter_cmd = f"sh -c 'echo filter >> \"{marker.as_posix()}\"; cat'"
    _git(workspace, "config", "core.hooksPath", planted.as_posix())
    _git(workspace, "config", "core.fsmonitor", fsmonitor.as_posix())
    _git(workspace, "config", "filter.evil.clean", filter_cmd)
    _git(workspace, "config", "filter.evil.smudge", filter_cmd)
    _git(workspace, "config", "diff.evil.textconv", filter_cmd)
    _git(workspace, "config", "diff.external", filter_cmd)
    (workspace / ".gitattributes").write_text("* filter=evil diff=evil\n", encoding="utf-8")


def _assert_nothing_ran(marker: Path) -> None:
    ran = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert ran == "", f"repository-controlled commands ran: {ran.split()}"


async def test_planted_hooks_and_drivers_do_not_run_during_create_branch_and_push(
    tmp_path: Path,
) -> None:
    remote = _init_bare_remote(tmp_path / "remote.git")
    workspace = tmp_path / "workspace"
    baseline = _init_git_repo(workspace)
    marker = tmp_path / "marker.txt"
    _plant_everything(workspace, marker)
    (workspace / "README.md").write_text("Changed\n", encoding="utf-8")

    job = _job(
        implementation_baseline_commit_sha=baseline,
        repo_info=RepoInfo(
            source_kind=RepoSourceKind.LOCAL,
            branch="main",
            commit_sha=baseline,
            origin_url=str(remote),
            is_dirty=False,
            local_path=None,
        ),
    )
    _collect_implementation_diff_sync(workspace, baseline)
    created = await branch_prep.create_branch(job, workspace, None, None)
    job.branch_name = created.branch_name
    job.branch_commit_sha = created.commit_sha
    await branch_prep.push_branch(
        job,
        workspace,
        None,
        PushCredentials(
            auth_header="Authorization: Basic dXNlcjp0b2tlbg==", remote_url=str(remote)
        ),
    )

    _assert_nothing_ran(marker)
    # The branch was still created and pushed normally.
    assert _remote_ref_sha(remote, f"refs/heads/{created.branch_name}") == created.commit_sha


async def test_ambient_push_does_not_run_a_planted_pre_push_hook(tmp_path: Path) -> None:
    remote = _init_bare_remote(tmp_path / "remote.git")
    workspace = tmp_path / "workspace"
    baseline = _init_git_repo(workspace)
    (workspace / "README.md").write_text("Changed\n", encoding="utf-8")
    job = _job(
        implementation_baseline_commit_sha=baseline,
        repo_info=RepoInfo(
            source_kind=RepoSourceKind.LOCAL,
            branch="main",
            commit_sha=baseline,
            origin_url=str(remote),
            is_dirty=False,
            local_path=None,
        ),
    )
    created = await branch_prep.create_branch(job, workspace, None, None)
    job.branch_name = created.branch_name
    job.branch_commit_sha = created.commit_sha
    # Planted *after* create-branch, e.g. by a validation run in between.
    marker = tmp_path / "marker.txt"
    _write_executable(workspace / ".git" / "hooks" / "pre-push", _marker_script(marker, "pre-push"))

    await branch_prep.push_branch(job, workspace, None)

    _assert_nothing_ran(marker)


def test_untrusted_repo_config_is_stripped_but_the_clone_config_is_kept(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_git_repo(workspace)
    _git(workspace, "remote", "add", "origin", "https://example.com/acme/repo.git")
    _git(workspace, "config", "core.hooksPath", ".husky")
    _git(workspace, "config", "url.https://evil.example/.insteadOf", "https://example.com/")
    _git(workspace, "config", "include.path", "../elsewhere.cfg")
    _git(workspace, "config", "core.sshCommand", "touch pwned")
    _git(workspace, "config", "gpg.program", "touch pwned")
    _git(workspace, "config", "commit.gpgSign", "true")

    workspace_git.prepare_workspace_git(workspace)

    remaining = _git(workspace, "config", "--local", "--list", "--name-only").splitlines()
    assert "remote.origin.url" in remaining
    assert "user.email" in remaining
    for stripped in ("core.hookspath", "include.path", "core.sshcommand", "gpg.program"):
        assert stripped not in remaining
    assert not any(key.startswith("url.") for key in remaining)
    assert _git(workspace, "config", "--get", "remote.origin.url") == (
        "https://example.com/acme/repo.git"
    )


def test_a_workspace_whose_git_dir_is_not_a_plain_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _init_git_repo(elsewhere)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A gitfile redirects git to another repository (and its config/hooks).
    (workspace / ".git").write_text(f"gitdir: {(elsewhere / '.git').as_posix()}\n")

    with pytest.raises(workspace_git.UnsafeWorkspaceGitError):
        workspace_git.prepare_workspace_git(workspace)


def test_a_commondir_redirect_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_git_repo(workspace)
    (workspace / ".git" / "commondir").write_text(str(tmp_path / "elsewhere"), encoding="utf-8")

    with pytest.raises(workspace_git.UnsafeWorkspaceGitError):
        workspace_git.prepare_workspace_git(workspace)


async def test_create_branch_fails_typed_on_an_unsafe_git_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    baseline = _init_git_repo(workspace)
    (workspace / "README.md").write_text("Changed\n", encoding="utf-8")
    (workspace / ".git" / "commondir").write_text(str(tmp_path / "elsewhere"), encoding="utf-8")

    with pytest.raises(branch_prep.AppError) as excinfo:
        await branch_prep.create_branch(
            _job(implementation_baseline_commit_sha=baseline), workspace, None, None
        )
    assert excinfo.value.code == branch_prep.ErrorCode.BRANCH_CREATION_FAILED


def test_the_no_hooks_dir_is_empty_and_outside_any_workspace(tmp_path: Path) -> None:
    command = workspace_git.workspace_git_command(tmp_path, "status")
    hooks_override = next(part for part in command if part.startswith("core.hooksPath="))
    hooks_dir = Path(hooks_override.split("=", 1)[1])
    assert hooks_dir.is_dir()
    assert list(hooks_dir.iterdir()) == []
    assert "core.fsmonitor=false" in command
