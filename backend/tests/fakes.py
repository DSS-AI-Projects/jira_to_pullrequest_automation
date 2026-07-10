"""Shared fake job steps for runner/API tests."""

from __future__ import annotations

from pathlib import Path

from app.jobs.models import AgentUsage
from app.jobs.runner import JobSteps, PlanResult
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
    async def fetch_ticket(ticket_key: str) -> TicketData:
        return TicketData(key=ticket_key, summary="A ticket", description="Do the thing.")

    async def clone_repo(job_id: str, repo_url: str, workdir: Path) -> Path:
        return workdir / job_id

    async def build_repo_map(clone_path: Path) -> RepoMap:
        return RepoMap(text="a.py\n", file_count=1)

    async def generate_plan(ticket: TicketData, repo_map: RepoMap, clone_path: Path) -> PlanResult:
        return PlanResult(plan=sample_plan(), usage=AgentUsage(duration_seconds=0.1))

    return JobSteps(
        fetch_ticket=fetch_ticket,
        clone_repo=clone_repo,
        build_repo_map=build_repo_map,
        generate_plan=generate_plan,
    )
