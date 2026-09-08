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
    CORRECTING = "CORRECTING"
    REVALIDATING = "REVALIDATING"
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


class RequirementSource(StrEnum):
    JIRA = "JIRA"
    DOCUMENT = "DOCUMENT"  # uploaded PDF — see document_fetch.py


class RepoSourceKind(StrEnum):
    REMOTE = "REMOTE"
    LOCAL = "LOCAL"
    LOCAL_FOLDER = "LOCAL_FOLDER"  # plain folder, no .git — see repo_clone.py


class RepoInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: RepoSourceKind
    # None for LOCAL_FOLDER: a plain folder has no git branch to report.
    branch: str | None
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
    requirement_source: RequirementSource = RequirementSource.JIRA
    requirement_document_name: str | None = None
    repo_url: str
    planning_notes: str | None = None
    state: JobState = JobState.QUEUED
    error: JobError | None = None
    # A short, rolling window of what the planning/implementation agent is
    # currently doing (tool calls only — "Reading X.java", not the model's
    # own narration; see app/steps/agent_progress.py), so the status screen
    # can show something more useful than the coarse job state while a slow
    # step is running. Reset at the start of each agent-backed step; capped
    # to the most recent entries by _append_activity() in app/jobs/runner.py.
    activity_log: list[str] = []
    repo_info: RepoInfo | None = None
    workspace_path: str | None = None
    plan: Plan | None = None
    # Recorded whenever the harness returns a result, including a failed one
    # (invalid/malformed output, budget exceeded, etc.) — an agent call that
    # completed still spent real Anthropic tokens/cost, so a failed job's cost
    # is never silently dropped. Only truly outcome-less failures (a timeout
    # or transport error before any harness result came back) leave these
    # unset, since there's genuinely nothing to report. See runner.py's
    # AppError.usage handling and app/steps/{plan,implement}_agent.py.
    usage: AgentUsage | None = None
    implementation_usage: AgentUsage | None = None
    implementation_result: ImplementationResult | None = None
    implementation_diff: ImplementationDiff | None = None
    validation_results: list[ValidationResult] = []
    implementation_clarifications: str | None = None
    implementation_approved_at: datetime | None = None
    implementation_started_at: datetime | None = None
    implementation_finished_at: datetime | None = None
    # The pre-implementation git SHA, persisted so a later correction pass can
    # diff against the same baseline the original implementation used —
    # without this, a correction run (a separate pipeline invocation) has no
    # reliable way to reproduce the diff boundary.
    implementation_baseline_commit_sha: str | None = None
    # A validation-failure correction is capped at one attempt per job (see
    # ErrorCode.VALIDATION_CORRECTION_NOT_AVAILABLE) and is strictly
    # best-effort: implementation_result/implementation_diff from the
    # original pass are never regressed by a failed or partial correction.
    implementation_correction_attempted: bool = False
    implementation_correction_result: ImplementationResult | None = None
    implementation_correction_error: JobError | None = None
    # Cost/token usage for the correction pass, recorded whether it succeeds
    # or fails — same rationale as usage/implementation_usage below: a failed
    # agent call still spends real Anthropic credits.
    implementation_correction_usage: AgentUsage | None = None
    # Set once a branch has been created and the reviewed diff committed to
    # it inside the isolated workspace (never pushed anywhere — see
    # ErrorCode.BRANCH_CREATION_NOT_AVAILABLE / BRANCH_CREATION_FAILED). The
    # endpoint refuses a second call once this is set: unlike validation
    # correction there's no LLM budget to protect, but re-creating after a
    # *successful* commit would mean re-pointing the branch at a fresh
    # checkout of the baseline, discarding the working tree that commit
    # already absorbed — get it right once instead. A rejected branch name
    # or commit message never reaches git, so it's always safely retryable.
    branch_name: str | None = None
    branch_commit_sha: str | None = None
    branch_created_at: datetime | None = None
    # Set once the branch has been pushed to the repo's real remote (never
    # the isolated workspace's own "origin", which for a LOCAL job points at
    # the user's local source path, not the actual remote — see
    # push_branch() in app/steps/branch_prep.py). Unlike branch creation this
    # is safely retryable on failure: pushing has no LLM cost to protect, and
    # a push failure never touches the local branch/commit, so nothing is
    # ever lost by trying again (optionally with a different branch name).
    branch_pushed_at: datetime | None = None
    branch_push_remote_url: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(
        cls,
        ticket_key: str,
        repo_url: str,
        owner_user_id: str | None = None,
        planning_notes: str | None = None,
        requirement_source: RequirementSource = RequirementSource.JIRA,
        requirement_document_name: str | None = None,
    ) -> Job:
        now = datetime.now(UTC)
        return cls(
            id=uuid.uuid4().hex,
            owner_user_id=owner_user_id,
            ticket_key=ticket_key,
            requirement_source=requirement_source,
            requirement_document_name=requirement_document_name,
            repo_url=repo_url,
            planning_notes=planning_notes,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
