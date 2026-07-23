"""Lazy, step-scoped access to secret env values.

Every secret the app touches MUST be obtained through this module: reading a
value here registers it with the log redactor (see logging.py), which is what
makes security invariant 2 ("secrets never logged") mechanical rather than
hopeful. Secrets are never stored in app-wide settings (see config.py).

This module is also half of the future-OAuth seam: when Jira auth moves to
OAuth, only steps/jira_auth.py and this module change.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

_registered: set[str] = set()

# Token-shaped string patterns, shared by the log redactor (invariant 2) and
# form-input validation (invariant 1).
TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Atlassian API token
    re.compile(r"ATATT[0-9A-Za-z_\-=+/]{10,}"),
    # Anthropic API key
    re.compile(r"sk-ant-[0-9A-Za-z_\-]{10,}"),
    # GitHub tokens (classic + fine-grained)
    re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    # GitLab / Bitbucket tokens
    re.compile(r"glpat-[0-9A-Za-z_\-]{15,}"),
    re.compile(r"BBDC-[0-9A-Za-z_\-]{10,}"),
    # HTTP auth headers
    re.compile(r"(?i)\b(?:basic|bearer)\s+[0-9A-Za-z+/_\-.=]{16,}"),
    # userinfo credentials embedded in URLs (https://user:pass@host)
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    # private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def looks_token_shaped(text: str) -> bool:
    """True if the text contains anything resembling a credential."""
    return any(pattern.search(text) for pattern in TOKEN_PATTERNS)


def register_secret(value: str) -> None:
    """Make the redactor aware of a secret value without storing it anywhere else."""
    if value and len(value) >= 8:
        _registered.add(value)


def registered_secrets() -> frozenset[str]:
    return frozenset(_registered)


def _read(name: str) -> str | None:
    value = os.environ.get(name)
    if not value and _ENV_FILE.exists():
        value = dotenv_values(_ENV_FILE).get(name) or None
    if value:
        register_secret(value)
    return value


def get_jira_api_token() -> str | None:
    """Read the Jira API token. Call only inside the Jira fetch step."""
    return _read("JIRA_API_TOKEN")


def get_jira_oauth_client_secret() -> str | None:
    """Read the Jira OAuth client secret for Atlassian 3LO flows."""
    return _read("JIRA_OAUTH_CLIENT_SECRET")


def get_jira_oauth_encryption_key() -> str | None:
    """Read the Fernet key used to encrypt persisted Jira OAuth tokens."""
    return _read("JIRA_OAUTH_ENCRYPTION_KEY")


def get_github_oauth_client_secret() -> str | None:
    """Read the GitHub OAuth client secret for per-user repository access."""
    return _read("GITHUB_OAUTH_CLIENT_SECRET")


def get_github_oauth_encryption_key() -> str | None:
    """Read the Fernet key used to encrypt persisted GitHub OAuth tokens."""
    return _read("GITHUB_OAUTH_ENCRYPTION_KEY")


def get_anthropic_api_key() -> str | None:
    """Read the Anthropic API key. Call only inside the planning agent step."""
    return _read("ANTHROPIC_API_KEY")
