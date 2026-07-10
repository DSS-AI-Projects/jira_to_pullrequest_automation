"""Non-secret application settings.

Secrets (JIRA_API_TOKEN, ANTHROPIC_API_KEY) are deliberately NOT fields here —
they are read lazily inside the step that needs them via core.secrets, so no
app-wide object ever holds them (security invariant 5).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Jira (non-secret parts; the token stays in core.secrets)
    jira_base_url: str | None = None
    jira_email: str | None = None

    # Repo input validation (security invariant 5)
    allowed_git_hosts: list[str] = ["github.com"]
    preconfigured_repos_file: Path = BACKEND_ROOT / "repos.config.json"

    # Runtime data
    workdir: Path = BACKEND_ROOT / "var" / "workdir"
    db_path: Path = BACKEND_ROOT / "var" / "jobs.db"

    # Frontend origin for CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # Planning agent budget (recorded per job; enforced in the agent step)
    agent_model: str | None = None  # None = harness default; override via env
    agent_max_turns: int = 40
    agent_timeout_seconds: int = 600
    agent_max_file_reads: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()
