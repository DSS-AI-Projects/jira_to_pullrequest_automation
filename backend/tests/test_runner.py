"""Runner failure contract: typed errors, terminal states, no leaks."""

import dataclasses
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DEFAULT_MESSAGES, AppError, ErrorCode
from app.jobs.models import (
    AgentUsage,
    ImplementationChange,
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


def init_git_workspace(path: Path) -> None:
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

    async def failing_fetch(job: Job, store: JobStore) -> TicketData:
        del job, store
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
        init_git_workspace(clone_path)
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

    async def implement_changes(job: Job, workspace_path: Path) -> ImplementationStepResult:
        del job
        (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md",
                        action="modify",
                        rationale="Update the greeting text to match the approved plan.",
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(
                input_tokens=120,
                output_tokens=80,
                total_cost_usd=0.02,
                num_turns=2,
                duration_seconds=1.2,
            ),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_changes,
    )
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
    assert final.implementation_diff is not None
    assert final.implementation_diff.overall_patch
    assert final.implementation_diff.files[0].path == "README.md"
    assert "-Hello AI Agentic World" in final.implementation_diff.files[0].patch
    assert "+Hello Back To World" in final.implementation_diff.files[0].patch
    assert final.implementation_usage is not None
    assert final.implementation_usage.total_cost_usd == 0.02
    assert final.validation_results
    assert final.validation_results[0].status == ValidationStatus.SKIPPED
    assert final.implementation_result.summary
    assert final.implementation_started_at is not None
    assert final.implementation_finished_at is not None


async def test_implementation_diff_is_captured_even_when_changes_are_committed(
    tmp_path: Path,
) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch=ticket_key,
                commit_sha="g" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def implement_and_commit(job: Job, workspace_path: Path) -> ImplementationStepResult:
        del job
        (workspace_path / "README.md").write_text("Hello Committed World\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace_path), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(workspace_path), "commit", "-m", "Apply approved changes"],
            check=True,
            capture_output=True,
            text=True,
        )
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting and committed the change.",
                changed_files=[],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=0.2),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_and_commit,
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
    assert final.state == JobState.IMPLEMENTATION_READY
    assert final.implementation_diff is not None
    assert final.implementation_diff.files
    assert final.implementation_diff.files[0].path == "README.md"
    assert "-Hello AI Agentic World" in final.implementation_diff.files[0].patch
    assert "+Hello Committed World" in final.implementation_diff.files[0].patch


async def test_implementation_diff_captures_new_untracked_files(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch=ticket_key,
                commit_sha="h" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def implement_create_new_file(job: Job, workspace_path: Path) -> ImplementationStepResult:
        del job
        # The real implement agent has no Bash tool, so a brand-new file it
        # writes is never `git add`-ed — it stays untracked, which `git diff`
        # ignores unless something stages it first.
        (workspace_path / "new_module.py").write_text(
            "def greet() -> str:\n    return 'hello'\n", encoding="utf-8"
        )
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Added a new greeting module.",
                changed_files=[
                    ImplementationChange(
                        path="new_module.py",
                        action="create",
                        rationale="New module requested by the ticket.",
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=0.3),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_create_new_file,
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
    assert final.state == JobState.IMPLEMENTATION_READY
    assert final.implementation_diff is not None
    assert "new_module.py" in final.implementation_diff.overall_patch
    assert any(f.path == "new_module.py" for f in final.implementation_diff.files)
    new_file = next(f for f in final.implementation_diff.files if f.path == "new_module.py")
    assert "+def greet" in new_file.patch


async def test_claimed_changes_with_no_actual_diff_is_implementation_invalid(
    tmp_path: Path,
) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch=ticket_key,
                commit_sha="i" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def implement_without_touching_disk(
        job: Job, workspace_path: Path
    ) -> ImplementationStepResult:
        # Simulates a tool call that silently failed: the agent's structured
        # output claims a file was changed, but nothing was actually written.
        del job, workspace_path
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md",
                        action="modify",
                        rationale="Update the greeting text to match the approved plan.",
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=0.4),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_without_touching_disk,
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
    assert final.error.code == ErrorCode.IMPLEMENTATION_INVALID
    assert final.error.stage == JobState.IMPLEMENTING
    # a failed consistency check must not leave a misleading hollow result
    assert final.implementation_result is None
    assert final.implementation_diff is None


async def test_implementation_app_error_yields_typed_failure_with_stage(tmp_path: Path) -> None:
    store, settings, job = make_env(tmp_path)

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
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
        init_git_workspace(clone_path)
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
        init_git_workspace(clone_path)
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
