"""Runner failure contract: typed errors, terminal states, no leaks."""

import dataclasses
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DEFAULT_MESSAGES, AppError, ErrorCode
from app.jobs.models import Job, JobState
from app.jobs.runner import run_job
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData
from tests.fakes import make_fake_steps

SECRET = "ATATT" + "y" * 30


def make_env(tmp_path: Path) -> tuple[JobStore, Settings, Job]:
    store = JobStore(tmp_path / "jobs.db")
    settings = Settings(_env_file=None, workdir=tmp_path / "workdir")  # type: ignore[call-arg]
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo")
    store.create(job)
    return store, settings, job


async def test_happy_path_reaches_plan_ready(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)
    await run_job(job.id, store, settings, make_fake_steps())
    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.PLAN_READY
    assert final.plan is not None
    assert final.usage is not None
    assert final.error is None


async def test_app_error_in_step_yields_typed_failure_with_stage(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def failing_fetch(ticket_key: str) -> TicketData:
        raise AppError(ErrorCode.TICKET_NOT_FOUND, internal_detail=f"jira said 404 {SECRET}")

    steps = dataclasses.replace(make_fake_steps(), fetch_ticket=failing_fetch)
    await run_job(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.FAILED
    assert final.error is not None
    assert final.error.code == ErrorCode.TICKET_NOT_FOUND
    assert final.error.stage == JobState.FETCHING_TICKET
    assert SECRET not in final.error.message  # internal detail never reaches the record


async def test_unexpected_crash_yields_generic_internal_error(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def crashing_clone(job_id: str, repo_url: str, workdir: Path) -> Path:
        raise RuntimeError(f"boom with {SECRET}")

    steps = dataclasses.replace(make_fake_steps(), clone_repo=crashing_clone)
    await run_job(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.FAILED
    assert final.error is not None
    assert final.error.code == ErrorCode.INTERNAL
    assert final.error.message == DEFAULT_MESSAGES[ErrorCode.INTERNAL]
    assert final.error.stage == JobState.CLONING_REPO
    assert SECRET not in final.error.message
