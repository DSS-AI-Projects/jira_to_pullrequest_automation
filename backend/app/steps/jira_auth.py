"""Jira authentication source — THE seam for a future OAuth swap.

Shared mode uses Jira Cloud Basic auth from env (email + API token), read lazily
so the token exists only inside the fetch step's call stack, never in app-wide
state. Delegated mode uses a per-user Atlassian OAuth connection stored
encrypted at rest.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from app.auth.jira_oauth import build_delegated_api_base, get_user_access_token
from app.core import secrets
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job
from app.jobs.store import JobStore


@dataclass(frozen=True)
class JiraAuth:
    base_url: str  # no trailing slash
    auth_header: str  # full Authorization header value


def get_shared_jira_auth(settings: Settings) -> JiraAuth:
    token = secrets.get_jira_api_token()  # lazy read; auto-registered with the redactor
    if not settings.jira_base_url or not settings.jira_email or not token:
        raise AppError(ErrorCode.JIRA_CONFIG_MISSING)
    encoded = base64.b64encode(f"{settings.jira_email}:{token}".encode()).decode()
    secrets.register_secret(encoded)  # the b64 form embeds the token — redact it too
    return JiraAuth(
        base_url=settings.jira_base_url.rstrip("/"),
        auth_header=f"Basic {encoded}",
    )


async def get_jira_auth(job: Job, store: JobStore, settings: Settings) -> JiraAuth:
    if settings.jira_oauth_enabled and job.owner_user_id:
        delegated = await get_user_access_token(job.owner_user_id, store, settings)
        if delegated is not None:
            connection, access_token = delegated
            return JiraAuth(
                base_url=build_delegated_api_base(connection.site.id),
                auth_header=f"Bearer {access_token}",
            )
        if (
            not settings.jira_base_url
            or not settings.jira_email
            or not secrets.get_jira_api_token()
        ):
            raise AppError(
                ErrorCode.JIRA_CONFIG_MISSING,
                user_message=(
                    "Connect your Jira account before planning, or configure shared Jira "
                    "credentials on the server."
                ),
            )
    return get_shared_jira_auth(settings)
