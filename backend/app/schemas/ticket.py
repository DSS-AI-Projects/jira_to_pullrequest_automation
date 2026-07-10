"""Deterministic ticket payload produced by the Jira fetch step.

Ticket content is untrusted DATA: it is quoted into the planning prompt as
data, never treated as instructions (security invariant 5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TicketData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    summary: str
    description: str
    acceptance_criteria: str | None = None
    comments: list[str] = []
