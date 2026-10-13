"""Workspace retention: finished jobs' clones/uploads are deleted after the
retention period, nothing else is ever touched, and deletion never leaves the
configured roots."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.models import (
    TERMINAL_STATES,
    AgentUsage,
    ImplementationDiff,
    Job,
    JobState,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
    ValidationStatus,
)
from app.jobs.retention import confined_job_dir, sweep_expired_workspaces, workspace_retention
from app.jobs.store import JobStore
from app.main import create_app
from tests.fakes import make_fake_steps, sample_plan

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
NON_FINISHED_STATES = [state for state in JobState if state not in TERMINAL_STATES]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        workdir=tmp_path / "var" / "workdir",
        document_upload_dir=tmp_path / "var" / "uploads",
        workspace_retention_enabled=True,
        workspace_retention_days=14,
    )


@pytest.fixture
def store() -> Iterator[JobStore]:
    job_store = JobStore(":memory:")
    yield job_store
    job_store.close()


def _add_job(
    store: JobStore,
    settings: Settings,
    *,
    state: JobState,
    age: timedelta,
    job_id: str | None = None,
) -> Job:
    """A job last changed `age` before NOW, with a populated workspace and
    upload directory on disk."""
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo.git")
    if job_id is not None:
        job.id = job_id
    job.state = state
    job.updated_at = NOW - age
    workspace = settings.workdir / job.id / "repo"
    if job_id is None:
        (workspace / ".git" / "objects").mkdir(parents=True)
        (workspace / "src.py").write_text("print('client code')\n", encoding="utf-8")
        # Git object files are read-only; deletion must still succeed.
        obj = workspace / ".git" / "objects" / "abc"
        obj.write_bytes(b"blob")
        obj.chmod(0o444)
        upload = settings.document_upload_dir / job.id
        upload.mkdir(parents=True)
        (upload / "requirement.pdf").write_bytes(b"%PDF-1.4")
    job.workspace_path = str(workspace)
    job.usage = AgentUsage(total_cost_usd=0.42, duration_seconds=3.0)
    job.implementation_diff = ImplementationDiff(overall_patch="diff --git a/x b/x")
    store.create(job)  # create() keeps updated_at as given; save() would bump it
    return job


def _dirs_exist(settings: Settings, job_id: str) -> tuple[bool, bool]:
    return (
        (settings.workdir / job_id).exists(),
        (settings.document_upload_dir / job_id).exists(),
    )


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_finished_job_past_retention_is_swept(
    store: JobStore, settings: Settings, state: JobState
) -> None:
    job = _add_job(store, settings, state=state, age=timedelta(days=15))

    removed = sweep_expired_workspaces(store, settings, now=NOW)

    assert removed == [job.id]
    assert _dirs_exist(settings, job.id) == (False, False)
    stored = store.get(job.id)
    assert stored is not None
    assert stored.workspace_removed_at == NOW
    assert stored.state == state
    # The record and its history are kept.
    assert stored.usage is not None and stored.usage.total_cost_usd == 0.42
    assert stored.implementation_diff is not None
    # The roots themselves are never removed.
    assert settings.workdir.is_dir() and settings.document_upload_dir.is_dir()


def test_finished_job_within_retention_is_kept(store: JobStore, settings: Settings) -> None:
    job = _add_job(store, settings, state=JobState.PLAN_READY, age=timedelta(days=13, hours=23))

    assert sweep_expired_workspaces(store, settings, now=NOW) == []

    assert _dirs_exist(settings, job.id) == (True, True)
    stored = store.get(job.id)
    assert stored is not None and stored.workspace_removed_at is None


def test_retention_days_setting_moves_the_threshold(store: JobStore, settings: Settings) -> None:
    job = _add_job(store, settings, state=JobState.FAILED, age=timedelta(days=3))
    assert sweep_expired_workspaces(store, settings, now=NOW) == []

    settings.workspace_retention_days = 2
    assert sweep_expired_workspaces(store, settings, now=NOW) == [job.id]
    assert _dirs_exist(settings, job.id) == (False, False)


@pytest.mark.parametrize("state", NON_FINISHED_STATES)
def test_unfinished_job_is_never_touched_however_old(
    store: JobStore, settings: Settings, state: JobState
) -> None:
    job = _add_job(store, settings, state=state, age=timedelta(days=365))

    assert sweep_expired_workspaces(store, settings, now=NOW) == []

    assert _dirs_exist(settings, job.id) == (True, True)
    stored = store.get(job.id)
    assert stored is not None and stored.workspace_removed_at is None


def test_job_changed_since_listing_is_not_marked(store: JobStore, settings: Settings) -> None:
    job = _add_job(store, settings, state=JobState.PLAN_READY, age=timedelta(days=30))
    (candidate,) = store.list_retention_candidates([JobState.PLAN_READY.value])

    # A request moves the job on between the listing and the mark.
    current = store.get(job.id)
    assert current is not None
    current.state = JobState.IMPLEMENTATION_QUEUED
    store.save(current)

    assert not store.mark_workspace_removed(
        job.id,
        states=[JobState.PLAN_READY.value],
        expected_updated_at=candidate.updated_at,
        removed_at=NOW,
    )
    stored = store.get(job.id)
    assert stored is not None
    assert stored.state == JobState.IMPLEMENTATION_QUEUED
    assert stored.workspace_removed_at is None


def test_leftover_files_of_a_swept_job_are_retried(store: JobStore, settings: Settings) -> None:
    job = _add_job(store, settings, state=JobState.IMPLEMENTATION_READY, age=timedelta(days=20))
    assert sweep_expired_workspaces(store, settings, now=NOW) == [job.id]

    # Simulate a half-finished earlier deletion (e.g. a locked file on Windows).
    leftover = settings.workdir / job.id / "repo"
    leftover.mkdir(parents=True)
    (leftover / "stray.txt").write_text("x", encoding="utf-8")

    assert sweep_expired_workspaces(store, settings, now=NOW + timedelta(hours=6)) == []
    assert not (settings.workdir / job.id).exists()
    stored = store.get(job.id)
    assert stored is not None and stored.workspace_removed_at == NOW  # not re-stamped


@pytest.mark.parametrize("job_id", ["", ".", "..", "a/b", "a\\b", "../escape", "C:"])
def test_confined_job_dir_rejects_ids_that_leave_the_root(tmp_path: Path, job_id: str) -> None:
    root = tmp_path / "workdir"
    root.mkdir()
    assert confined_job_dir(root, job_id) is None


def test_confined_job_dir_accepts_a_plain_child(tmp_path: Path) -> None:
    root = tmp_path / "workdir"
    root.mkdir()
    assert confined_job_dir(root, "abc123") == (root / "abc123").resolve()


def test_confined_job_dir_refuses_a_symlink_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "workdir"
    root.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    try:
        (root / "linkjob").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted here")
    assert confined_job_dir(root, "linkjob") is None


def test_sweep_never_deletes_outside_the_roots(store: JobStore, settings: Settings) -> None:
    # A job id that would resolve to the roots' parent ("var/") â€” which also
    # holds the database and every other job's files.
    sentinel = settings.workdir.parent / "jobs.db"
    settings.workdir.mkdir(parents=True)
    settings.document_upload_dir.mkdir(parents=True)
    sentinel.write_text("keep me", encoding="utf-8")
    bad = _add_job(store, settings, state=JobState.FAILED, age=timedelta(days=30), job_id="..")
    good = _add_job(store, settings, state=JobState.FAILED, age=timedelta(days=30))

    sweep_expired_workspaces(store, settings, now=NOW)

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert settings.workdir.is_dir() and settings.document_upload_dir.is_dir()
    assert _dirs_exist(settings, good.id) == (False, False)
    assert store.get(bad.id) is not None


async def test_lifespan_runs_a_first_sweep_at_startup(store: JobStore, settings: Settings) -> None:
    job = _add_job(store, settings, state=JobState.PLAN_READY, age=timedelta(days=400))

    async with workspace_retention(store, settings):
        for _ in range(100):
            stored = store.get(job.id)
            if stored is not None and stored.workspace_removed_at is not None:
                break
            await asyncio.sleep(0.02)

    assert _dirs_exist(settings, job.id) == (False, False)


async def test_lifespan_does_nothing_when_disabled(store: JobStore, settings: Settings) -> None:
    settings.workspace_retention_enabled = False
    job = _add_job(store, settings, state=JobState.PLAN_READY, age=timedelta(days=400))

    async with workspace_retention(store, settings):
        await asyncio.sleep(0.1)

    assert _dirs_exist(settings, job.id) == (True, True)


@pytest.fixture
def client(store: JobStore) -> Iterator[TestClient]:
    app = create_app(store=store, steps=make_fake_steps())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("state", "endpoint"),
    [
        (JobState.PLAN_READY, "implement"),
        (JobState.IMPLEMENTATION_READY, "correct-validation"),
        (JobState.IMPLEMENTATION_READY, "create-branch"),
        (JobState.IMPLEMENTATION_READY, "push-branch"),
    ],
)
def test_workspace_actions_on_a_swept_job_fail_with_a_clear_error(
    client: TestClient, store: JobStore, settings: Settings, state: JobState, endpoint: str
) -> None:
    job = _add_job(store, settings, state=state, age=timedelta(days=30))
    job.repo_info = RepoInfo(
        source_kind=RepoSourceKind.REMOTE,
        branch="main",
        commit_sha="a" * 40,
        origin_url="https://github.com/acme/repo.git",
        is_dirty=False,
    )
    job.plan = sample_plan()
    job.validation_results = [
        ValidationResult(name="lint", command="ruff", status=ValidationStatus.FAILED, summary="x")
    ]
    job.branch_name = "jira2pullreq/PROJ-1" if endpoint == "push-branch" else None
    job.workspace_removed_at = NOW
    store.save(job)

    response = client.post(f"/api/jobs/{job.id}/{endpoint}")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "IMPLEMENTATION_WORKSPACE_MISSING"
    assert "retention" in error["message"]


def test_repeat_push_of_a_swept_job_still_reports_the_earlier_push(
    client: TestClient, store: JobStore, settings: Settings
) -> None:
    job = _add_job(store, settings, state=JobState.IMPLEMENTATION_READY, age=timedelta(days=30))
    job.branch_name = "jira2pullreq/PROJ-1"
    job.branch_pushed_at = NOW - timedelta(days=30)
    job.branch_push_remote_url = "https://github.com/acme/repo.git"
    job.workspace_removed_at = NOW
    store.save(job)

    # Nothing to do, so no workspace needed: the earlier result comes back.
    repeat = client.post(f"/api/jobs/{job.id}/push-branch")
    assert repeat.status_code == 200
    assert repeat.json() == {
        "branch_name": "jira2pullreq/PROJ-1",
        "remote_url": "https://github.com/acme/repo.git",
        "compare_url": "https://github.com/acme/repo/compare/jira2pullreq/PROJ-1?expand=1",
    }

    # Pushing under a new name would need git, so it still fails.
    renamed = client.post(
        f"/api/jobs/{job.id}/push-branch", json={"branch_name": "jira2pullreq/PROJ-1-v2"}
    )
    assert renamed.status_code == 400
    assert renamed.json()["error"]["code"] == "IMPLEMENTATION_WORKSPACE_MISSING"
