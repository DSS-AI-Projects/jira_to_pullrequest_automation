"""Job step implementations.

Wired into the runner via default_steps(). Steps land one commit at a time;
unimplemented ones raise INTERNAL so a job still terminates cleanly.
"""

from __future__ import annotations

from pathlib import Path

from app.core.errors import AppError, ErrorCode
from app.jobs.runner import JobSteps, PlanResult
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData


def _not_implemented(step: str) -> AppError:
    return AppError(ErrorCode.INTERNAL, internal_detail=f"step '{step}' not implemented yet")


async def _fetch_ticket(ticket_key: str) -> TicketData:
    raise _not_implemented("fetch_ticket")


async def _clone_repo(job_id: str, repo_url: str, workdir: Path) -> Path:
    raise _not_implemented("clone_repo")


async def _build_repo_map(clone_path: Path) -> RepoMap:
    raise _not_implemented("build_repo_map")


async def _generate_plan(ticket: TicketData, repo_map: RepoMap, clone_path: Path) -> PlanResult:
    raise _not_implemented("generate_plan")


def default_steps() -> JobSteps:
    return JobSteps(
        fetch_ticket=_fetch_ticket,
        clone_repo=_clone_repo,
        build_repo_map=_build_repo_map,
        generate_plan=_generate_plan,
    )
