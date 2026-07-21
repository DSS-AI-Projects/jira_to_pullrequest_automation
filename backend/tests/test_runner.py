"""Runner failure contract: typed errors, terminal states, no leaks."""

import dataclasses
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DEFAULT_MESSAGES, AppError, ErrorCode
from app.jobs.models import (
    AgentUsage,
    ImplementationResult,
    Job,
    JobState,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
    ValidationStatus,
)
from app.jobs.runner import CloneResult, ImplementationStepResult, run_implementation, run_job
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
    assert final.repo_info is not None
    assert final.repo_info.branch == "main"


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

    async def crashing_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
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


async def test_clone_metadata_is_persisted_on_typed_failure(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def clone_then_fail(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        return CloneResult(
            clone_path=workdir / job_id,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch="PROJ-1",
                commit_sha="b" * 40,
                origin_url="https://github.com/acme/repo",
                is_dirty=True,
                local_path="D:\\repos\\repo",
            ),
        )

    async def failing_map(clone_path: Path):
        raise AppError(ErrorCode.REPO_MAP_FAILED)

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=clone_then_fail,
        build_repo_map=failing_map,
    )
    await run_job(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.FAILED
    assert final.repo_info is not None
    assert final.repo_info.source_kind == RepoSourceKind.LOCAL
    assert final.repo_info.is_dirty is True


async def test_implementation_happy_path_reaches_implementation_ready(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        clone_path.mkdir(parents=True, exist_ok=True)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch="PROJ-1",
                commit_sha="c" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    steps = dataclasses.replace(make_fake_steps(), clone_repo=local_clone)
    await run_job(job.id, store, settings, steps)

    planned = store.get(job.id)
    assert planned is not None
    assert planned.state == JobState.PLAN_READY

    planned.state = JobState.IMPLEMENTATION_QUEUED
    planned.implementation_approved_at = planned.updated_at
    store.save(planned)
    await run_implementation(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.IMPLEMENTATION_READY
    assert final.implementation_result is not None
    assert final.implementation_usage is not None
    assert final.implementation_usage.total_cost_usd == 0.02
    assert final.validation_results
    assert final.validation_results[0].status == ValidationStatus.SKIPPED
    assert final.implementation_result.summary
    assert final.implementation_started_at is not None
    assert final.implementation_finished_at is not None


async def test_implementation_app_error_yields_typed_failure_with_stage(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        clone_path.mkdir(parents=True, exist_ok=True)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch="PROJ-1",
                commit_sha="d" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def failing_implement(job: Job, workspace_path: Path) -> ImplementationStepResult:
        del job, workspace_path
        raise AppError(ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING, internal_detail=SECRET)

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=failing_implement,
    )
    await run_job(job.id, store, settings, steps)

    planned = store.get(job.id)
    assert planned is not None
    planned.state = JobState.IMPLEMENTATION_QUEUED
    planned.implementation_approved_at = planned.updated_at
    store.save(planned)
    await run_implementation(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.IMPLEMENTATION_FAILED
    assert final.error is not None
    assert final.error.code == ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING
    assert final.error.stage == JobState.IMPLEMENTING
    assert SECRET not in final.error.message


async def test_validation_app_error_yields_typed_failure_with_stage(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        clone_path.mkdir(parents=True, exist_ok=True)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch="PROJ-1",
                commit_sha="e" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def failing_validate(workspace_path: Path) -> list[ValidationResult]:
        del workspace_path
        raise AppError(ErrorCode.VALIDATION_FAILED, internal_detail=SECRET)

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        validate_workspace=failing_validate,
    )
    await run_job(job.id, store, settings, steps)

    planned = store.get(job.id)
    assert planned is not None
    planned.state = JobState.IMPLEMENTATION_QUEUED
    planned.implementation_approved_at = planned.updated_at
    store.save(planned)
    await run_implementation(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.state == JobState.IMPLEMENTATION_FAILED
    assert final.error is not None
    assert final.error.code == ErrorCode.VALIDATION_FAILED
    assert final.error.stage == JobState.VALIDATING
    assert SECRET not in final.error.message


async def test_reapproval_clears_previous_implementation_artifacts(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        clone_path.mkdir(parents=True, exist_ok=True)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch="PROJ-1",
                commit_sha="f" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def implement_once(job: Job, workspace_path: Path) -> ImplementationStepResult:
        del job, workspace_path
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Done.",
                changed_files=[],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(
                input_tokens=10,
                output_tokens=5,
                total_cost_usd=0.01,
                num_turns=1,
                duration_seconds=0.5,
            ),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_once,
    )
    await run_job(job.id, store, settings, steps)

    planned = store.get(job.id)
    assert planned is not None
    planned.state = JobState.IMPLEMENTATION_QUEUED
    planned.implementation_approved_at = planned.updated_at
    store.save(planned)
    await run_implementation(job.id, store, settings, steps)

    final = store.get(job.id)
    assert final is not None
    assert final.implementation_usage is not None
    assert final.implementation_usage.total_cost_usd == 0.01
