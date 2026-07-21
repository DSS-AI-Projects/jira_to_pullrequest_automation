"""Job step implementations, wired into the runner via default_steps()."""

from __future__ import annotations

from app.jobs.runner import JobSteps
from app.steps.implement_agent import implement_plan
from app.steps.jira_fetch import fetch_ticket
from app.steps.plan_agent import generate_plan
from app.steps.repo_clone import clone_repo
from app.steps.repo_map import build_repo_map
from app.steps.validation_runner import validate_workspace


def default_steps() -> JobSteps:
    return JobSteps(
        fetch_ticket=fetch_ticket,
        clone_repo=clone_repo,
        build_repo_map=build_repo_map,
        generate_plan=generate_plan,
        implement_plan=implement_plan,
        validate_workspace=validate_workspace,
    )
