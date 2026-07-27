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
    IMPLEMENTATION_QUEUED = "IMPLEMENTATION_QUEUED"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    IMPLEMENTATION_READY = "IMPLEMENTATION_READY"
    IMPLEMENTATION_FAILED = "IMPLEMENTATION_FAILED"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset(
    {
        JobState.PLAN_READY,
        JobState.IMPLEMENTATION_READY,
        JobState.IMPLEMENTATION_FAILED,
        JobState.FAILED,
    }
)


class JobError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str  # user-safe; the status screen renders it verbatim
    stage: JobState  # which step failed


class RepoSourceKind(StrEnum):
    REMOTE = "REMOTE"
    LOCAL = "LOCAL"


class RepoInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: RepoSourceKind
    branch: str
    commit_sha: str
    origin_url: str | None = None
    is_dirty: bool
    local_path: str | None = None


class AgentUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None
    duration_seconds: float
    cached: bool = False  # True when served from the plan cache (no API call)


class ImplementationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: str
    rationale: str


class ImplementationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    changed_files: list[ImplementationChange]
    warnings: list[str] = []
    follow_up_questions: list[str] = []


class ImplementationDiffFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    patch: str
    additions: int | None = None
    deletions: int | None = None
    is_binary: bool = False


class ImplementationDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_patch: str
    files: list[ImplementationDiffFile] = []


class ValidationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    status: ValidationStatus
    summary: str
    output_excerpt: str | None = None


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    owner_user_id: str | None = None
    ticket_key: str
    repo_url: str
    state: JobState = JobState.QUEUED
    error: JobError | None = None
    repo_info: RepoInfo | None = None
    workspace_path: str | None = None
    plan: Plan | None = None
    usage: AgentUsage | None = None
    implementation_usage: AgentUsage | None = None
    implementation_result: ImplementationResult | None = None
    implementation_diff: ImplementationDiff | None = None
    validation_results: list[ValidationResult] = []
    implementation_approved_at: datetime | None = None
    implementation_started_at: datetime | None = None
    implementation_finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(cls, ticket_key: str, repo_url: str, owner_user_id: str | None = None) -> Job:
        now = datetime.now(UTC)
        return cls(
            id=uuid.uuid4().hex,
            owner_user_id=owner_user_id,
            ticket_key=ticket_key,
            repo_url=repo_url,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
