"""Jira authentication source — THE seam for a future OAuth swap.

Today: Jira Cloud Basic auth from env (email + API token), read lazily so the
token exists only inside the fetch step's call stack, never in app-wide state.
A later OAuth milestone replaces get_jira_auth()'s body with a token exchange;
nothing else in the app changes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from app.core import secrets
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class JiraAuth:
    base_url: str  # no trailing slash
    auth_header: str  # full Authorization header value


def get_jira_auth(settings: Settings) -> JiraAuth:
    token = secrets.get_jira_api_token()  # lazy read; auto-registered with the redactor
    if not settings.jira_base_url or not settings.jira_email or not token:
        raise AppError(ErrorCode.JIRA_CONFIG_MISSING)
    encoded = base64.b64encode(f"{settings.jira_email}:{token}".encode()).decode()
    secrets.register_secret(encoded)  # the b64 form embeds the token — redact it too
    return JiraAuth(
        base_url=settings.jira_base_url.rstrip("/"),
        auth_header=f"Basic {encoded}",
    )
