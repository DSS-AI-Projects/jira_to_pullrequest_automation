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
    allow_local_repos: bool = False
    allowed_local_repo_roots: list[Path] = []
    allow_dirty_local_repos: bool = False
    require_local_branch_ticket_match: bool = False

    # Runtime data
    workdir: Path = BACKEND_ROOT / "var" / "workdir"
    db_path: Path = BACKEND_ROOT / "var" / "jobs.db"

    # App authentication
    auth_enabled: bool = False
    auth_allow_dev_login: bool = True
    auth_session_cookie_name: str = "jira2pullreq_session"
    auth_session_ttl_hours: int = 12
    auth_session_cookie_secure: bool = False
    auth_trusted_proxy_enabled: bool = False
    auth_trusted_proxy_sources: list[str] = []
    auth_trusted_email_header: str = "X-Auth-Request-Email"
    auth_trusted_name_header: str = "X-Auth-Request-Name"
    auth_trusted_subject_header: str = "X-Auth-Request-User"
    auth_trusted_provider_name: str = "trusted-proxy"
    auth_admin_emails: list[str] = []

    # Frontend origin for CORS
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # Clone
    clone_timeout_seconds: int = 300

    # Repo map caps (keeps the agent's starting context bounded)
    repo_map_max_files: int = 4000
    repo_map_max_file_bytes: int = 262_144
    repo_map_max_chars: int = 60_000

    # Planning agent budget (recorded per job; enforced in the agent step)
    agent_model: str = "claude-opus-4-8"
    agent_max_turns: int = 40
    agent_timeout_seconds: int = 600
    agent_max_budget_usd: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
