"""Startup recovery: jobs a previous process left mid-flight end terminal."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import DEFAULT_MESSAGES, ErrorCode
from app.jobs.models import (
    TERMINAL_STATES,
    ImplementationChange,
    ImplementationDiff,
    ImplementationDiffFile,
    ImplementationResult,
    ImplementationRevision,
    Job,
    JobState,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
    ValidationStatus,
)
from app.jobs.runner import recover_interrupted_jobs
from app.jobs.store import JobStore
from app.main import create_app
from tests.fakes import make_fake_steps, sample_plan

INTERRUPTED_MESSAGE = DEFAULT_MESSAGES[ErrorCode.JOB_INTERRUPTED]

PLAN_PIPELINE_STATES = [
    JobState.QUEUED,
    JobState.FETCHING_TICKET,
    JobState.CLONING_REPO,
    JobState.MAPPING_REPO,
    JobState.PLANNING,
]
IMPLEMENTATION_STATES = [
    JobState.IMPLEMENTATION_QUEUED,
    JobState.IMPLEMENTING,
    JobState.VALIDATING,
]
CORRECTION_STATES = [JobState.CORRECTING, JobState.REVALIDATING]


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout


def init_git_workspace(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("Hello\n", encoding="utf-8")
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "Initial commit")
    return _git(path, "rev-parse", "HEAD").strip()


def add_job(store: JobStore, state: JobState) -> Job:
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo")
    store.create(job)
    job.state = state
    store.save(job)
    return job


def add_implemented_job(store: JobStore, workspace: Path, state: JobState) -> Job:
    """A job past planning, with a git workspace and a recorded baseline."""
    baseline = init_git_workspace(workspace)
    job = add_job(store, state)
    job.plan = sample_plan()
    job.repo_info = RepoInfo(
        source_kind=RepoSourceKind.REMOTE,
        branch="main",
        commit_sha=baseline,
        is_dirty=False,
    )
    job.workspace_path = str(workspace)
    job.implementation_baseline_commit_sha = baseline
    store.save(job)
    return job


@pytest.mark.parametrize("state", PLAN_PIPELINE_STATES)
async def test_interrupted_plan_pipeline_job_fails(tmp_path: Path, state: JobState) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = add_job(store, state)

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.FAILED
    assert recovered.error is not None
    assert recovered.error.code == ErrorCode.JOB_INTERRUPTED
    assert recovered.error.message == INTERRUPTED_MESSAGE
    assert recovered.error.stage == state


@pytest.mark.parametrize("state", IMPLEMENTATION_STATES)
async def test_interrupted_implementation_job_fails_and_keeps_partial_edits(
    tmp_path: Path, state: JobState
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "workspace"
    job = add_implemented_job(store, workspace, state)
    # An edit the agent had already written before the process stopped.
    (workspace / "README.md").write_text("Hello\nPartial change\n", encoding="utf-8")

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.IMPLEMENTATION_FAILED
    assert recovered.error is not None
    assert recovered.error.code == ErrorCode.JOB_INTERRUPTED
    assert recovered.error.message == INTERRUPTED_MESSAGE
    assert recovered.error.stage == state
    # The approved plan survives, and the partial edit is surfaced exactly as
    # on any other implementation failure.
    assert recovered.plan == job.plan
    assert recovered.implementation_diff is not None
    assert [file.path for file in recovered.implementation_diff.files] == ["README.md"]
    assert "+Partial change" in recovered.implementation_diff.overall_patch


async def test_interrupted_implementation_without_a_workspace_still_fails(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = add_job(store, JobState.IMPLEMENTATION_QUEUED)
    job.plan = sample_plan()
    job.workspace_path = str(tmp_path / "gone")
    store.save(job)

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.IMPLEMENTATION_FAILED
    assert recovered.implementation_diff is None


@pytest.mark.parametrize("state", CORRECTION_STATES)
async def test_interrupted_correction_never_regresses_the_implementation(
    tmp_path: Path, state: JobState
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "workspace"
    job = add_implemented_job(store, workspace, state)
    (workspace / "README.md").write_text("Hello\nImplemented\n", encoding="utf-8")
    original_result = ImplementationResult(
        summary="Implemented the change.",
        changed_files=[ImplementationChange(path="README.md", action="modify", rationale="Ticket")],
    )
    original_validation = [
        ValidationResult(
            name="pytest",
            command="pytest",
            status=ValidationStatus.FAILED,
            summary="1 failed",
        )
    ]
    job.implementation_result = original_result
    job.implementation_diff = ImplementationDiff(
        overall_patch="+Implemented\n",
        files=[ImplementationDiffFile(path="README.md", patch="+Implemented\n")],
    )
    job.validation_results = original_validation
    # The correction agent had its slot and was running.
    job.implementation_correction_started_at = datetime.now(UTC)
    store.save(job)

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.IMPLEMENTATION_READY
    assert recovered.error is None
    assert recovered.implementation_correction_attempted is True
    assert recovered.implementation_correction_error is not None
    assert recovered.implementation_correction_error.code == ErrorCode.JOB_INTERRUPTED
    assert recovered.implementation_correction_error.message == INTERRUPTED_MESSAGE
    assert recovered.implementation_correction_error.stage == state
    # The working implementation stays as it was; the diff is re-read from
    # the workspace so it matches what a later branch commit would contain.
    assert recovered.implementation_result == original_result
    assert recovered.validation_results == original_validation
    assert recovered.implementation_diff is not None
    assert "+Implemented" in recovered.implementation_diff.overall_patch


@pytest.mark.parametrize("state", [JobState.REVISING, JobState.REVALIDATING])
async def test_interrupted_review_round_keeps_its_edits_and_the_job_ready(
    tmp_path: Path, state: JobState
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "workspace"
    job = add_implemented_job(store, workspace, state)
    (workspace / "README.md").write_text("Hello\nImplemented\n", encoding="utf-8")
    snapshot_before = (_git(workspace, "add", "-A") + _git(workspace, "write-tree")).strip()
    (workspace / "review.md").write_text("Round edit\n", encoding="utf-8")
    original_result = ImplementationResult(summary="Implemented the change.")
    job.implementation_result = original_result
    job.implementation_revisions = [
        ImplementationRevision(
            number=1,
            feedback="Add review.md",
            requested_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
            snapshot_before=snapshot_before,
        )
    ]
    store.save(job)

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.IMPLEMENTATION_READY
    assert recovered.error is None
    assert recovered.implementation_result == original_result
    revision = recovered.implementation_revisions[0]
    assert revision.finished_at is not None
    assert revision.error is not None
    assert revision.error.code == ErrorCode.JOB_INTERRUPTED
    assert revision.error.stage == state
    assert revision.counts_toward_limit
    assert revision.diff is not None
    assert [file.path for file in revision.diff.files] == ["review.md"]
    assert recovered.implementation_diff is not None
    assert {file.path for file in recovered.implementation_diff.files} == {
        "README.md",
        "review.md",
    }


async def test_review_round_interrupted_while_waiting_does_not_count(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = add_implemented_job(store, tmp_path / "workspace", JobState.REVISING)
    job.implementation_revisions = [
        ImplementationRevision(number=1, feedback="x", requested_at=datetime.now(UTC))
    ]
    store.save(job)

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.IMPLEMENTATION_READY
    assert recovered.implementation_revisions[0].error is not None
    assert recovered.revisions_used == 0


async def test_terminal_jobs_are_left_untouched(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    jobs = [add_job(store, state) for state in sorted(TERMINAL_STATES)]
    before = {job.id: store.get(job.id) for job in jobs}

    assert await recover_interrupted_jobs(store) == 0

    for job in jobs:
        assert store.get(job.id) == before[job.id]


async def test_recovers_every_interrupted_job_in_one_pass(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    planning = add_job(store, JobState.PLANNING)
    implementing = add_job(store, JobState.IMPLEMENTING)
    correcting = add_job(store, JobState.CORRECTING)
    done = add_job(store, JobState.PLAN_READY)

    assert await recover_interrupted_jobs(store) == 3
    # A second startup finds nothing left to do.
    assert await recover_interrupted_jobs(store) == 0

    states = {
        job.id: recovered.state
        for job in (planning, implementing, correcting, done)
        if (recovered := store.get(job.id)) is not None
    }
    assert states == {
        planning.id: JobState.FAILED,
        implementing.id: JobState.IMPLEMENTATION_FAILED,
        correcting.id: JobState.IMPLEMENTATION_READY,
        done.id: JobState.PLAN_READY,
    }


def test_app_startup_recovers_interrupted_jobs(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = add_job(store, JobState.CLONING_REPO)
    app = create_app(store=store, steps=make_fake_steps())

    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.get(f"/api/jobs/{job.id}").json()

    assert body["state"] == "FAILED"
    assert body["error"] == {
        "code": "JOB_INTERRUPTED",
        "message": INTERRUPTED_MESSAGE,
        "stage": "CLONING_REPO",
    }
