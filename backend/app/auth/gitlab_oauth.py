"""GitLab OAuth helpers for per-user repository provider connections.

Mirrors github_oauth.py's shape closely, with two deliberate differences
from GitHub's implementation, not just a re-skin:

1. **Configurable instance URL** (`settings.gitlab_instance_url`, default
   `https://gitlab.com`) — GitLab is commonly self-hosted, unlike this app's
   GitHub integration, which is hardcoded to github.com. Every GitLab API
   call is built off this one setting.
2. **Token refresh.** GitLab access tokens expire (2h by default) and issue
   rotating refresh tokens; GitHub's OAuth Apps don't expire tokens in this
   app's configuration, so github_oauth.py never needed a refresh path. This
   follows jira_oauth.py's proven `_is_token_stale()` / refresh-before-use
   pattern instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.auth.models import (
    GitLabRepositoryListResponse,
    GitLabRepositorySummary,
    RepoHostingAuthKind,
    RepoHostingConnectCallbackResponse,
    RepoHostingConnection,
    RepoHostingConnectionInfo,
    RepoHostingConnectStartResponse,
    RepoHostingProvider,
    User,
)
from app.core import secrets
from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import AppError, ErrorCode
from app.jobs.store import JobStore

_TOKEN_REFRESH_SKEW_SECONDS = 60


@dataclass(frozen=True)
class GitLabTokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


def gitlab_oauth_is_configured(settings: Settings) -> bool:
    return bool(
        settings.gitlab_oauth_enabled
        and settings.gitlab_oauth_client_id
        and settings.gitlab_oauth_callback_url
        and secrets.get_gitlab_oauth_client_secret()
        and secrets.get_gitlab_oauth_encryption_key()
    )


def _require_gitlab_oauth_configured(settings: Settings) -> tuple[str, str]:
    client_id = settings.gitlab_oauth_client_id
    callback_url = settings.gitlab_oauth_callback_url
    client_secret = secrets.get_gitlab_oauth_client_secret()
    encryption_key = secrets.get_gitlab_oauth_encryption_key()
    if client_id and callback_url and client_secret and encryption_key:
        return client_id, callback_url

    missing: list[str] = []
    if not settings.gitlab_oauth_enabled:
        missing.append("GITLAB_OAUTH_ENABLED")
    if not client_id:
        missing.append("GITLAB_OAUTH_CLIENT_ID")
    if not callback_url:
        missing.append("GITLAB_OAUTH_CALLBACK_URL")
    if not client_secret:
        missing.append("GITLAB_OAUTH_CLIENT_SECRET")
    if not encryption_key:
        missing.append("GITLAB_OAUTH_ENCRYPTION_KEY")
    raise AppError(
        ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
        internal_detail="Missing GitLab OAuth configuration: " + ", ".join(missing),
    )


def create_gitlab_authorization_request(
    user: User, store: JobStore, settings: Settings
) -> RepoHostingConnectStartResponse:
    client_id, callback_url = _require_gitlab_oauth_configured(settings)
    oauth_state = store.create_provider_oauth_state(
        user.id, RepoHostingProvider.GITLAB, settings.gitlab_oauth_state_ttl_minutes
    )
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": " ".join(settings.gitlab_oauth_scopes),
            "state": oauth_state.state,
        }
    )
    authorize_url = f"{settings.gitlab_instance_url}/oauth/authorize"
    return RepoHostingConnectStartResponse(authorization_url=f"{authorize_url}?{params}")


async def complete_gitlab_authorization(
    *,
    user: User,
    store: JobStore,
    settings: Settings,
    code: str,
    state: str,
) -> RepoHostingConnectCallbackResponse:
    client_id, callback_url = _require_gitlab_oauth_configured(settings)
    if not store.consume_provider_oauth_state(state, user.id, RepoHostingProvider.GITLAB):
        raise AppError(ErrorCode.REPO_PROVIDER_STATE_INVALID)
    token_bundle = await _exchange_token(
        settings,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": secrets.get_gitlab_oauth_client_secret() or "",
            "code": code,
            "redirect_uri": callback_url,
        },
    )
    user_profile = await _fetch_current_user(settings, token_bundle.access_token)
    connection = RepoHostingConnection.new(
        user_id=user.id,
        provider=RepoHostingProvider.GITLAB,
        auth_kind=RepoHostingAuthKind.OAUTH_USER,
        account_name=str(user_profile["username"]),
        account_id=str(user_profile["id"]),
        account_url=str(user_profile["web_url"]),
        scopes=token_bundle.scopes or settings.gitlab_oauth_scopes,
        access_token_encrypted=encrypt_secret(token_bundle.access_token, provider="gitlab"),
        refresh_token_encrypted=(
            encrypt_secret(token_bundle.refresh_token, provider="gitlab")
            if token_bundle.refresh_token is not None
            else None
        ),
        access_token_expires_at=token_bundle.expires_at,
    )
    store.save_repo_hosting_connection(connection)
    return RepoHostingConnectCallbackResponse(
        connection=RepoHostingConnectionInfo.from_connection(connection)
    )


def _is_token_stale(expires_at: datetime | None) -> bool:
    if expires_at is None:
        # GitLab always returns expires_in for the authorization_code and
        # refresh_token grants, so this shouldn't happen for a connection
        # this module created — but never treat "unknown" as "still fresh".
        return True
    now = datetime.now(UTC) + timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS)
    return expires_at <= now


async def _get_valid_access_token(
    connection: RepoHostingConnection, store: JobStore, settings: Settings
) -> tuple[RepoHostingConnection, str]:
    if _is_token_stale(connection.access_token_expires_at):
        connection = await _refresh_connection(connection, store, settings)
    access_token = decrypt_secret(connection.access_token_encrypted or "", provider="gitlab")
    return connection, access_token


async def _refresh_connection(
    connection: RepoHostingConnection, store: JobStore, settings: Settings
) -> RepoHostingConnection:
    client_id, _callback_url = _require_gitlab_oauth_configured(settings)
    if connection.refresh_token_encrypted is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Your GitLab connection has expired. Reconnect your GitLab account.",
            internal_detail=f"missing refresh token for user {connection.user_id}",
        )
    token_bundle = await _exchange_token(
        settings,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": secrets.get_gitlab_oauth_client_secret() or "",
            "refresh_token": decrypt_secret(connection.refresh_token_encrypted, provider="gitlab"),
        },
    )
    connection.access_token_encrypted = encrypt_secret(token_bundle.access_token, provider="gitlab")
    if token_bundle.refresh_token is not None:
        connection.refresh_token_encrypted = encrypt_secret(
            token_bundle.refresh_token, provider="gitlab"
        )
    connection.access_token_expires_at = token_bundle.expires_at
    store.save_repo_hosting_connection(connection)
    return connection


async def list_gitlab_repositories(
    user: User, store: JobStore, settings: Settings
) -> GitLabRepositoryListResponse:
    connection = store.get_repo_hosting_connection(user.id, RepoHostingProvider.GITLAB)
    if connection is None or connection.access_token_encrypted is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Connect your GitLab account before loading repositories.",
        )

    try:
        _connection, access_token = await _get_valid_access_token(connection, store, settings)
    except AppError as exc:
        # A stored token that can no longer be decrypted or refreshed (e.g.
        # the encryption key rotated, or the refresh token was revoked) is
        # functionally the same as never having connected — surface the same
        # actionable, user-safe message instead of a bare INTERNAL error.
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Connect your GitLab account before loading repositories.",
            internal_detail=exc.internal_detail,
        ) from exc
    projects = await _fetch_user_projects(settings, access_token)
    return GitLabRepositoryListResponse(repos=projects)


async def _exchange_token(settings: Settings, payload: dict[str, str]) -> GitLabTokenBundle:
    token_url = f"{settings.gitlab_instance_url}/oauth/token"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                headers={"Accept": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"gitlab token exchange returned {response.status_code}",
        )
    body = response.json()
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail="gitlab token exchange missing access_token",
        )
    refresh_token = str(body.get("refresh_token") or "").strip() or None
    expires_in = body.get("expires_in")
    secrets.register_secret(access_token)
    if refresh_token is not None:
        secrets.register_secret(refresh_token)
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
    scope_text = body.get("scope")
    scopes = [scope for scope in str(scope_text or "").split(" ") if scope]
    return GitLabTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )


async def _fetch_current_user(settings: Settings, access_token: str) -> dict[str, Any]:
    user_url = f"{settings.gitlab_instance_url}/api/v4/user"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                user_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code in (401, 403):
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            user_message=(
                "GitLab rejected the delegated sign-in response. Reconnect your GitLab account."
            ),
            internal_detail=f"gitlab user lookup returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"gitlab user lookup returned {response.status_code}",
        )
    body = response.json()
    username = str(body.get("username") or "").strip()
    web_url = str(body.get("web_url") or "").strip()
    account_id = body.get("id")
    if not username or not web_url or account_id is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail="gitlab user payload missing username/web_url/id",
        )
    return {"username": username, "web_url": web_url, "id": int(account_id)}


async def _fetch_user_projects(
    settings: Settings, access_token: str
) -> list[GitLabRepositorySummary]:
    projects_url = f"{settings.gitlab_instance_url}/api/v4/projects"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                projects_url,
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "membership": "true",
                    "per_page": "100",
                    "order_by": "last_activity_at",
                },
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code in (401, 403):
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            user_message=(
                "GitLab rejected repository access for this connection. "
                "Reconnect your GitLab account."
            ),
            internal_detail=f"gitlab project listing returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"gitlab project listing returned {response.status_code}",
        )

    payload: list[dict[str, Any]] = response.json()
    projects: list[GitLabRepositorySummary] = []
    for item in payload:
        namespace: dict[str, Any] = item.get("namespace") or {}
        # GitLab has no boolean "private" field — visibility is one of
        # "private" | "internal" | "public"; only "public" is not private.
        visibility = str(item.get("visibility") or "private")
        projects.append(
            GitLabRepositorySummary(
                id=int(item["id"]),
                name=str(item["name"]),
                path_with_namespace=str(item["path_with_namespace"]),
                web_url=str(item["web_url"]),
                http_url_to_repo=str(item["http_url_to_repo"]),
                default_branch=(
                    str(item["default_branch"]).strip() if item.get("default_branch") else None
                ),
                namespace=str(namespace.get("path") or namespace.get("name") or ""),
                private=visibility != "public",
            )
        )
    return projects
