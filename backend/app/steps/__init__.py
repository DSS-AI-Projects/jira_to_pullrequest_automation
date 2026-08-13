"""Job step implementations, wired into the runner via default_steps()."""

from __future__ import annotations

from app.jobs.models import Job, RequirementSource
from app.jobs.runner import JobSteps
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData
from app.steps.document_fetch import fetch_requirement_document
from app.steps.implement_agent import implement_plan
from app.steps.jira_fetch import fetch_ticket as fetch_jira_ticket
from app.steps.plan_agent import generate_plan
from app.steps.repo_clone import clone_repo
from app.steps.repo_map import build_repo_map
from app.steps.validation_runner import validate_workspace


async def fetch_requirement(job: Job, store: JobStore) -> TicketData:
    """Dispatch to the Jira fetch or the document-extraction step, whichever
    the job was created with — both produce the same TicketData shape, so
    nothing downstream (planning) needs to know which source was used."""
    if job.requirement_source == RequirementSource.DOCUMENT:
        return await fetch_requirement_document(job, store)
    return await fetch_jira_ticket(job, store)


def default_steps() -> JobSteps:
    return JobSteps(
        fetch_ticket=fetch_requirement,
        clone_repo=clone_repo,
        build_repo_map=build_repo_map,
        generate_plan=generate_plan,
        implement_plan=implement_plan,
        validate_workspace=validate_workspace,
    )
