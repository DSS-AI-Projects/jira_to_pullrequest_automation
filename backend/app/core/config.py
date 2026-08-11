"""Non-secret application settings.

Secrets (JIRA_API_TOKEN, ANTHROPIC_API_KEY) are deliberately NOT fields here —
they are read lazily inside the step that needs them via core.secrets, so no
app-wide object ever holds them (security invariant 5).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    jira_oauth_enabled: bool = False
    jira_oauth_client_id: str | None = None
    jira_oauth_callback_url: str | None = None
    jira_oauth_scopes: list[str] = ["read:jira-work", "offline_access"]
    jira_oauth_state_ttl_minutes: int = 10
    github_oauth_enabled: bool = False
    github_oauth_client_id: str | None = None
    github_oauth_callback_url: str | None = None
    github_oauth_scopes: list[str] = ["repo", "read:user"]
    github_oauth_state_ttl_minutes: int = 10
    gitlab_oauth_enabled: bool = False
    gitlab_oauth_client_id: str | None = None
    gitlab_oauth_callback_url: str | None = None
    gitlab_oauth_scopes: list[str] = ["api", "read_user"]

    # Repo input validation (security invariant 5)
    allowed_git_hosts: list[str] = ["github.com"]
    preconfigured_repos_file: Path = BACKEND_ROOT / "repos.config.json"
    allow_local_repos: bool = False
    allowed_local_repo_roots: list[Path] = []
    allow_dirty_local_repos: bool = False
    require_local_branch_ticket_match: bool = False
    # Separate opt-in from allow_local_repos: a plain folder (no .git) has no
    # branch/dirty history to check, so accepting one is a materially weaker
    # guarantee than a real git work tree.
    allow_local_non_git_folders: bool = False

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

    # Agent models — per phase so planning can run on a cheaper tier than
    # implementation (token-saving; planning is analysis, not code-writing).
    agent_plan_model: str = "claude-sonnet-5"
    agent_implement_model: str = "claude-opus-4-8"
    # Reasoning depth (low|medium|high|xhigh|max). "medium" is the cost/quality
    # sweet spot for planning; raise for hard tickets.
    agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Per-phase turn + cost caps. Planning is bounded tighter than implementation.
    agent_plan_max_turns: int = 20
    agent_implement_max_turns: int = 40
    agent_plan_max_budget_usd: float = 1.0
    # None disables the implementation-phase budget cap entirely (SDK default).
    agent_implement_max_budget_usd: float | None = None
    agent_timeout_seconds: int = 600
    # Token-saving controls (see docs). Stub returns a canned plan with NO API
    # call (pipeline testing without credits). Cache reuses a prior plan for the
    # same ticket+repo inputs. Repo-doc injection front-loads CLAUDE.md/AGENTS.md
    # so the agent needs fewer exploration reads.
    agent_plan_stub: bool = False
    agent_plan_cache_enabled: bool = True
    agent_repo_doc_max_chars: int = 8000
    # Deterministic repo digest (zero-token orientation), computed once per repo
    # state and cached, injected into planning so the agent explores less.
    agent_repo_digest_enabled: bool = True
    agent_repo_digest_cache_enabled: bool = True
    agent_repo_digest_max_chars: int = 3000


@lru_cache
def get_settings() -> Settings:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return Settings(_env_file=None)  # type: ignore[call-arg]
    return Settings()
