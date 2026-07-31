"""Shared fake job steps for runner/API tests."""

from __future__ import annotations

from pathlib import Path

from app.jobs.models import (
    AgentUsage,
    ImplementationChange,
    ImplementationResult,
    Job,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
    ValidationStatus,
)
from app.jobs.runner import CloneResult, ImplementationStepResult, JobSteps, PlanResult
from app.jobs.store import JobStore
from app.schemas.plan import Plan
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData


def sample_plan() -> Plan:
    return Plan.model_validate(
        {
            "schema_version": 1,
            "summary": "Do the thing.",
            "ticket_type": "feature",
            "impacted_files": [{"path": "a.py", "reason": "entry point"}],
            "proposed_changes": [
                {"file": "a.py", "action": "modify", "description": "Add the thing."}
            ],
            "test_strategy": "Unit tests.",
            "risks": [],
            "open_questions": [],
        }
    )


def make_fake_steps() -> JobSteps:
    async def fetch_ticket(job: Job, store: JobStore) -> TicketData:
        del store
        return TicketData(key=job.ticket_key, summary="A ticket", description="Do the thing.")

    async def clone_repo(job_id: str, ticket_key: str, repo_url: str, workdir: Path) -> CloneResult:
        return CloneResult(
            clone_path=workdir / job_id,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.REMOTE,
                branch="main",
                commit_sha="a" * 40,
                origin_url=repo_url,
                is_dirty=False,
            ),
        )

    async def build_repo_map(clone_path: Path) -> RepoMap:
        return RepoMap(text="a.py\n", file_count=1)

    async def generate_plan(
        ticket: TicketData, repo_map: RepoMap, clone_path: Path, planning_notes: str | None
    ) -> PlanResult:
        del planning_notes
        return PlanResult(plan=sample_plan(), usage=AgentUsage(duration_seconds=0.1))

    async def implement_plan(job: Job, workspace_path: Path) -> ImplementationStepResult:
        return ImplementationStepResult(
            result=ImplementationResult(
                summary=f"Prepared implementation output for {job.ticket_key}.",
                changed_files=[
                    ImplementationChange(
                        path="a.py",
                        action="modify",
                        rationale=f"Placeholder implementation result for {workspace_path.name}.",
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

    async def validate_workspace(workspace_path: Path) -> list[ValidationResult]:
        return [
            ValidationResult(
                name="validation-profile",
                command="",
                status=ValidationStatus.SKIPPED,
                summary=f"No validation configured for {workspace_path.name}.",
            )
        ]

    return JobSteps(
        fetch_ticket=fetch_ticket,
        clone_repo=clone_repo,
        build_repo_map=build_repo_map,
        generate_plan=generate_plan,
        implement_plan=implement_plan,
        validate_workspace=validate_workspace,
    )
