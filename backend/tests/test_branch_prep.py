"""Branch preparation: naming, validation, and the git plumbing that creates
a branch and commits the reviewed diff inside the isolated workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job, RepoInfo, RepoSourceKind, RequirementSource
from app.steps import branch_prep
from tests.fakes import sample_plan


def _job(**overrides: object) -> Job:
    job = Job.new(ticket_key="KAN-34", repo_url="git@github.com:acme/repo.git")
    job.plan = sample_plan()
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("Hello AI Agentic World\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(path), "branch", "-M", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "Initial commit"],
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_bare_remote(path: Path) -> Path:
    """A real, local-filesystem "remote" — `git push`/`git ls-remote` treat a
    bare repo at a plain path exactly like any other remote, no test server
    needed."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True, text=True)
    return path


def _remote_ref_sha(remote_path: Path, ref: str) -> str | None:
    completed = subprocess.run(
        ["git", "ls-remote", str(remote_path), ref],
        check=True,
        capture_output=True,
        text=True,
    )
    line = completed.stdout.strip()
    return line.split("\t", 1)[0] if line else None


def test_default_branch_name_uses_ticket_key_for_jira_jobs() -> None:
    job = _job()
    assert branch_prep.default_branch_name(job) == "jira2pullreq/KAN-34"


def test_default_branch_name_slugifies_summary_for_document_jobs() -> None:
    job = _job(ticket_key="DOC-A1B2C3D4", requirement_source=RequirementSource.DOCUMENT)
    assert job.plan is not None
    job.plan.summary = "Add a Program Type filter!"
    assert (
        branch_prep.default_branch_name(job)
        == "jira2pullreq/doc-a1b2c3d4-add-a-program-type-filter"
    )


def test_default_commit_message_combines_ticket_and_summary() -> None:
    job = _job()
    assert branch_prep.default_commit_message(job) == "KAN-34: Do the thing."


async def test_create_branch_commits_the_staged_diff(tmp_path: Path) -> None:
    baseline = _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
    (tmp_path / "new_file.py").write_text("x = 1\n", encoding="utf-8")

    job = _job(implementation_baseline_commit_sha=baseline)
    result = await branch_prep.create_branch(job, tmp_path, None, None)

    assert result.branch_name == "jira2pullreq/KAN-34"
    assert result.commit_sha != baseline

    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "KAN-34: Do the thing." in log

    branches = subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches == "jira2pullreq/KAN-34"


async def test_create_branch_honors_custom_name_and_message(tmp_path: Path) -> None:
    baseline = _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")

    job = _job(implementation_baseline_commit_sha=baseline)
    result = await branch_prep.create_branch(
        job, tmp_path, "custom/my-branch", "A custom commit message"
    )

    assert result.branch_name == "custom/my-branch"
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "A custom commit message" in log


async def test_create_branch_rejects_an_invalid_name(tmp_path: Path) -> None:
    baseline = _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")

    job = _job(implementation_baseline_commit_sha=baseline)
    with pytest.raises(AppError) as excinfo:
        await branch_prep.create_branch(job, tmp_path, "bad..name", None)
    assert excinfo.value.code == ErrorCode.BRANCH_NAME_INVALID


async def test_create_branch_fails_when_nothing_to_commit(tmp_path: Path) -> None:
    baseline = _init_git_repo(tmp_path)

    job = _job(implementation_baseline_commit_sha=baseline)
    with pytest.raises(AppError) as excinfo:
        await branch_prep.create_branch(job, tmp_path, None, None)
    assert excinfo.value.code == ErrorCode.BRANCH_CREATION_FAILED
    assert "no changes to commit" in excinfo.value.user_message.lower()


async def test_create_branch_falls_back_to_head_without_a_stored_baseline(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")

    job = _job(implementation_baseline_commit_sha=None)
    result = await branch_prep.create_branch(job, tmp_path, None, None)

    assert result.branch_name == "jira2pullreq/KAN-34"


async def _prepared_job_and_workspace(
    tmp_path: Path, *, remote_url: str | None
) -> tuple[Job, Path]:
    workspace_path = tmp_path / "workspace"
    baseline = _init_git_repo(workspace_path)
    (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")

    job = _job(
        implementation_baseline_commit_sha=baseline,
        repo_info=RepoInfo(
            source_kind=RepoSourceKind.LOCAL,
            branch="main",
            commit_sha=baseline,
            origin_url=remote_url,
            is_dirty=False,
            local_path=None,
        ),
    )
    created = await branch_prep.create_branch(job, workspace_path, None, None)
    job.branch_name = created.branch_name
    job.branch_commit_sha = created.commit_sha
    return job, workspace_path


async def test_push_branch_pushes_to_the_real_remote(tmp_path: Path) -> None:
    remote_path = _init_bare_remote(tmp_path / "remote.git")
    job, workspace_path = await _prepared_job_and_workspace(tmp_path, remote_url=str(remote_path))

    result = await branch_prep.push_branch(job, workspace_path, None)

    assert result.branch_name == "jira2pullreq/KAN-34"
    assert result.remote_url == str(remote_path)
    pushed_sha = _remote_ref_sha(remote_path, "refs/heads/jira2pullreq/KAN-34")
    assert pushed_sha == job.branch_commit_sha


async def test_push_branch_rejects_when_no_remote_configured(tmp_path: Path) -> None:
    job, workspace_path = await _prepared_job_and_workspace(tmp_path, remote_url=None)

    with pytest.raises(AppError) as excinfo:
        await branch_prep.push_branch(job, workspace_path, None)
    assert excinfo.value.code == ErrorCode.BRANCH_PUSH_NOT_AVAILABLE


async def test_push_branch_rejects_when_no_branch_created_yet(tmp_path: Path) -> None:
    remote_path = _init_bare_remote(tmp_path / "remote.git")
    workspace_path = tmp_path / "workspace"
    baseline = _init_git_repo(workspace_path)
    job = _job(
        implementation_baseline_commit_sha=baseline,
        repo_info=RepoInfo(
            source_kind=RepoSourceKind.LOCAL,
            branch="main",
            commit_sha=baseline,
            origin_url=str(remote_path),
            is_dirty=False,
            local_path=None,
        ),
    )
    assert job.branch_name is None

    with pytest.raises(AppError) as excinfo:
        await branch_prep.push_branch(job, workspace_path, None)
    assert excinfo.value.code == ErrorCode.BRANCH_PUSH_NOT_AVAILABLE


async def test_push_branch_rejects_a_name_that_already_exists_on_the_remote(
    tmp_path: Path,
) -> None:
    remote_path = _init_bare_remote(tmp_path / "remote.git")
    job, workspace_path = await _prepared_job_and_workspace(tmp_path, remote_url=str(remote_path))
    await branch_prep.push_branch(job, workspace_path, None)

    # A second, independent job trying to push the *same* branch name (e.g.
    # two attempts at the same ticket) must not silently overwrite it.
    other_job, other_workspace = await _prepared_job_and_workspace(
        tmp_path, remote_url=str(remote_path)
    )
    with pytest.raises(AppError) as excinfo:
        await branch_prep.push_branch(other_job, other_workspace, None)
    assert excinfo.value.code == ErrorCode.BRANCH_PUSH_REJECTED


async def test_push_branch_renames_and_pushes_when_a_different_name_is_given(
    tmp_path: Path,
) -> None:
    remote_path = _init_bare_remote(tmp_path / "remote.git")
    job, workspace_path = await _prepared_job_and_workspace(tmp_path, remote_url=str(remote_path))

    result = await branch_prep.push_branch(job, workspace_path, "custom/renamed")

    assert result.branch_name == "custom/renamed"
    assert _remote_ref_sha(remote_path, "refs/heads/custom/renamed") == job.branch_commit_sha
    # The old name was never pushed at all.
    assert _remote_ref_sha(remote_path, "refs/heads/jira2pullreq/KAN-34") is None
