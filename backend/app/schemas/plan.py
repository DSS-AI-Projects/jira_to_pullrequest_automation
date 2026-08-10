"""The plan object — a versioned contract shared with the frontend and consumed
by the future implement step. Treat as an API: change only with a
schema_version bump, and regenerate schema/plan.schema.json + the frontend
types (scripts/check.py fails on drift).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2

StoryPoints = Literal[1, 2, 3, 5, 8, 13, 21]

# Standard Scrum/agile Fibonacci-like sizing scale.
STORY_POINT_SCALE: tuple[StoryPoints, ...] = (1, 2, 3, 5, 8, 13, 21)


class TicketType(StrEnum):
    FEATURE = "feature"
    BUG = "bug"
    REFACTOR = "refactor"
    CHORE = "chore"
    UNKNOWN = "unknown"


class ComplexityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class ChangeAction(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class ImpactedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Repo-relative path")
    reason: str = Field(min_length=1, description="Why this file is impacted")


class ProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, description="Repo-relative path (may be a new file)")
    action: ChangeAction
    description: str = Field(
        min_length=1, description="What to change and why, specific enough to implement"
    )


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Accepts 1 so plans persisted before estimated_story_points/complexity_level
    # existed still deserialize (as None on those two fields) rather than
    # failing to load. New plans are always produced at SCHEMA_VERSION (2).
    schema_version: Literal[1, 2] = SCHEMA_VERSION
    summary: str = Field(min_length=1, description="One-paragraph plan summary")
    ticket_type: TicketType
    # Optional (rather than required) so pre-existing plans without an estimate
    # still deserialize; None renders as "Not estimated" in the UI, mirroring
    # how usage.total_cost_usd renders "Not recorded" when absent.
    estimated_story_points: StoryPoints | None = Field(
        default=None,
        description=(
            "Effort estimate on the standard Fibonacci-like Scrum scale "
            "(1=trivial, 21=very large), based on the scope of proposed_changes"
        ),
    )
    complexity_level: ComplexityLevel | None = Field(
        default=None,
        description=(
            "Overall implementation complexity. HIGH or VERY_HIGH signals this "
            "ticket should likely be broken into smaller subtasks before starting"
        ),
    )
    impacted_files: list[ImpactedFile]
    proposed_changes: list[ProposedChange] = Field(
        min_length=1, description="A plan must propose at least one concrete change"
    )
    test_strategy: str = Field(min_length=1)
    risks: list[str]
    open_questions: list[str]
