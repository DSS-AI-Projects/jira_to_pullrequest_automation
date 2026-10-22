"""Load and spend limits for agent runs.

Two independent controls for a shared deployment:

- **AgentSlots** caps how many agent-backed runs (the plan pipeline, an
  implementation, a validation correction) execute at once
  (MAX_CONCURRENT_AGENT_RUNS). Each run holds a slot for its whole body, so
  a job waiting for one stays in the state its route left it in (QUEUED /
  IMPLEMENTATION_QUEUED / CORRECTING). The semaphore lives in memory: a
  restart starts with every slot free, and recover_interrupted_jobs() has
  already moved any job that was waiting to a terminal state.
- **check_spend_limit** refuses a new run once the job owner's recorded
  agent cost over the last 24 hours reaches USER_DAILY_BUDGET_USD (off by
  default). Costs come from the job records themselves (usage,
  implementation_usage, implementation_correction_usage), so a run counts
  once its cost is recorded — including failed runs (see CLAUDE.md, "Cost is
  recorded even when the job fails").
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import AgentUsage, Job
from app.jobs.store import JobStore

logger = get_logger(__name__)

SPEND_WINDOW = timedelta(hours=24)


class AgentSlots:
    """A FIFO-fair cap on concurrently running agent jobs.

    asyncio.Semaphore wakes waiters in arrival order, so jobs start in the
    order they were queued. `running`/`waiting` are for logging and tests.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.running = 0
        self.waiting = 0
        self._semaphore = asyncio.Semaphore(limit)

    @asynccontextmanager
    async def hold(self, job_id: str) -> AsyncGenerator[None]:
        if self.running >= self.limit or self.waiting:
            logger.info(
                "job %s waiting for an agent slot (%d running, %d waiting, limit %d)",
                job_id,
                self.running,
                self.waiting,
                self.limit,
            )
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._semaphore.release()


@asynccontextmanager
async def agent_slot(slots: AgentSlots | None, job_id: str) -> AsyncGenerator[None]:
    """Hold a slot from `slots`, or run unlimited when there is none (the
    runner functions called directly, as most tests do)."""
    if slots is None:
        yield
        return
    async with slots.hold(job_id):
        yield


@dataclass(frozen=True)
class SpendEvent:
    at: datetime
    cost_usd: float


def _cost(usage: AgentUsage | None) -> float:
    if usage is None:
        return 0.0
    return usage.total_cost_usd or 0.0


def spend_events(job: Job) -> list[SpendEvent]:
    """Each recorded agent cost on a job, stamped with when that run started.

    Planning is stamped with the job's creation time; implementation with
    implementation_started_at; the correction pass with
    implementation_correction_started_at. A record written before those
    timestamps existed falls back to the job's updated_at, which is never
    earlier than the real run, so the fallback can only over-count the window.
    """
    events: list[SpendEvent] = []
    for usage, at in (
        (job.usage, job.created_at),
        (job.implementation_usage, job.implementation_started_at or job.updated_at),
        (
            job.implementation_correction_usage,
            job.implementation_correction_started_at or job.updated_at,
        ),
    ):
        cost = _cost(usage)
        if cost > 0:
            events.append(SpendEvent(at=at, cost_usd=cost))
    return events


@dataclass(frozen=True)
class SpendStatus:
    spent_usd: float
    limit_usd: float
    # When enough of the window's spend will have aged out to bring the total
    # back under the limit; None while under the limit.
    resets_at: datetime | None

    @property
    def exceeded(self) -> bool:
        return self.spent_usd >= self.limit_usd


def spend_status(
    store: JobStore, owner_user_id: str, limit_usd: float, now: datetime | None = None
) -> SpendStatus:
    now = now or datetime.now(UTC)
    since = now - SPEND_WINDOW
    events = sorted(
        (
            event
            for job in store.list_owner_jobs_updated_since(owner_user_id, since)
            for event in spend_events(job)
            if event.at > since
        ),
        key=lambda event: event.at,
    )
    spent = sum(event.cost_usd for event in events)
    resets_at: datetime | None = None
    remaining = spent
    if spent >= limit_usd:
        for event in events:
            remaining -= event.cost_usd
            if remaining < limit_usd:
                resets_at = event.at + SPEND_WINDOW
                break
    return SpendStatus(spent_usd=spent, limit_usd=limit_usd, resets_at=resets_at)


def check_spend_limit(
    store: JobStore,
    owner_user_id: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Raise BUDGET_LIMIT_REACHED if the owner is at their rolling 24h limit.

    No-op when USER_DAILY_BUDGET_USD is unset or the job has no owner
    (AUTH_ENABLED=false). Admins are not exempt: the limit is about money, and
    an admin who needs more raises the setting.
    """
    limit = settings.user_daily_budget_usd
    if limit is None or owner_user_id is None:
        return
    status = spend_status(store, owner_user_id, limit, now)
    if not status.exceeded:
        return
    resets = (
        status.resets_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if status.resets_at is not None
        else "within 24 hours"
    )
    raise AppError(
        ErrorCode.BUDGET_LIMIT_REACHED,
        user_message=(
            f"You have used ${status.spent_usd:.2f} of your ${status.limit_usd:.2f} "
            "AI budget for the last 24 hours, so no new plan, implementation or fix "
            f"can start right now. More budget becomes available at {resets}."
        ),
        internal_detail=(
            f"owner {owner_user_id} spent {status.spent_usd:.4f} USD in 24h, "
            f"limit {status.limit_usd:.4f}"
        ),
    )
