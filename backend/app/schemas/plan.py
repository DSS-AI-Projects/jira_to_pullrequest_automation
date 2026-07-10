"""The plan object — a versioned contract shared with the frontend and consumed
by the future implement step. Treat as an API: change only with a
schema_version bump, and regenerate schema/plan.schema.json + the frontend
types (scripts/check.py fails on drift).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class TicketType(StrEnum):
    FEATURE = "feature"
    BUG = "bug"
    REFACTOR = "refactor"
    CHORE = "chore"
    UNKNOWN = "unknown"


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

    schema_version: Literal[1] = SCHEMA_VERSION
    summary: str = Field(min_length=1, description="One-paragraph plan summary")
    ticket_type: TicketType
    impacted_files: list[ImpactedFile]
    proposed_changes: list[ProposedChange] = Field(
        min_length=1, description="A plan must propose at least one concrete change"
    )
    test_strategy: str = Field(min_length=1)
    risks: list[str]
    open_questions: list[str]
