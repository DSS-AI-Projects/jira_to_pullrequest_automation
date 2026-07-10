"""Deterministic repo map produced by the tree-sitter step (no LLM)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RepoMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str  # rendered file tree + symbols, handed to the planning agent
    file_count: int
    symbol_count: int = 0
    truncated: bool = False
