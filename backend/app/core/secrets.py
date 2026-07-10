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
from pathlib import Path

from dotenv import dotenv_values

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

_registered: set[str] = set()


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


def get_anthropic_api_key() -> str | None:
    """Read the Anthropic API key. Call only inside the planning agent step."""
    return _read("ANTHROPIC_API_KEY")
