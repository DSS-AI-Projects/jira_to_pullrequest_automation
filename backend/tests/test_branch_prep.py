"""Branch preparation: naming, validation, and the git plumbing that creates
a branch and commits the reviewed diff inside the isolated workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job, RequirementSource
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
