"""Job record and state machine. Every job ends in a terminal state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.core.errors import ErrorCode
from app.schemas.plan import Plan


class JobState(StrEnum):
    QUEUED = "QUEUED"
    FETCHING_TICKET = "FETCHING_TICKET"
    CLONING_REPO = "CLONING_REPO"
    MAPPING_REPO = "MAPPING_REPO"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset({JobState.PLAN_READY, JobState.FAILED})


class JobError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str  # user-safe; the status screen renders it verbatim
    stage: JobState  # which step failed


class AgentUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None
    duration_seconds: float


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_key: str
    repo_url: str
    state: JobState = JobState.QUEUED
    error: JobError | None = None
    plan: Plan | None = None
    usage: AgentUsage | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(cls, ticket_key: str, repo_url: str) -> Job:
        now = datetime.now(UTC)
        return cls(
            id=uuid.uuid4().hex,
            ticket_key=ticket_key,
            repo_url=repo_url,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
