"""GitHub OAuth helpers for per-user repository provider connections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.auth.models import (
    GitHubRepositoryListResponse,
    GitHubRepositorySummary,
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

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_REPOS_URL = "https://api.github.com/user/repos"
_TOKEN_REFRESH_SKEW_SECONDS = 60


@dataclass(frozen=True)
class GitHubTokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


def github_oauth_is_configured(settings: Settings) -> bool:
    return bool(
        settings.github_oauth_enabled
        and settings.github_oauth_client_id
        and settings.github_oauth_callback_url
        and secrets.get_github_oauth_client_secret()
        and secrets.get_github_oauth_encryption_key()
    )


def _require_github_oauth_configured(settings: Settings) -> tuple[str, str]:
    client_id = settings.github_oauth_client_id
    callback_url = settings.github_oauth_callback_url
    client_secret = secrets.get_github_oauth_client_secret()
    encryption_key = secrets.get_github_oauth_encryption_key()
    if client_id and callback_url and client_secret and encryption_key:
        return client_id, callback_url

    missing: list[str] = []
    if not settings.github_oauth_enabled:
        missing.append("GITHUB_OAUTH_ENABLED")
    if not client_id:
        missing.append("GITHUB_OAUTH_CLIENT_ID")
    if not callback_url:
        missing.append("GITHUB_OAUTH_CALLBACK_URL")
    if not client_secret:
        missing.append("GITHUB_OAUTH_CLIENT_SECRET")
    if not encryption_key:
        missing.append("GITHUB_OAUTH_ENCRYPTION_KEY")
    raise AppError(
        ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
        internal_detail="Missing GitHub OAuth configuration: " + ", ".join(missing),
    )


def create_github_authorization_request(
    user: User, store: JobStore, settings: Settings
) -> RepoHostingConnectStartResponse:
    client_id, callback_url = _require_github_oauth_configured(settings)
    oauth_state = store.create_provider_oauth_state(
        user.id, RepoHostingProvider.GITHUB, settings.github_oauth_state_ttl_minutes
    )
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url,
            "scope": " ".join(settings.github_oauth_scopes),
            "state": oauth_state.state,
        }
    )
    return RepoHostingConnectStartResponse(authorization_url=f"{_AUTHORIZE_URL}?{params}")


async def complete_github_authorization(
    *,
    user: User,
    store: JobStore,
    settings: Settings,
    code: str,
    state: str,
) -> RepoHostingConnectCallbackResponse:
    client_id, callback_url = _require_github_oauth_configured(settings)
    if not store.consume_provider_oauth_state(state, user.id, RepoHostingProvider.GITHUB):
        raise AppError(ErrorCode.REPO_PROVIDER_STATE_INVALID)
    token_bundle = await _exchange_authorization_code(
        client_id=client_id,
        client_secret=secrets.get_github_oauth_client_secret() or "",
        callback_url=callback_url,
        code=code,
    )
    user_profile = await _fetch_current_user(token_bundle.access_token)
    connection = RepoHostingConnection.new(
        user_id=user.id,
        provider=RepoHostingProvider.GITHUB,
        auth_kind=RepoHostingAuthKind.OAUTH_USER,
        account_name=str(user_profile["login"]),
        account_id=str(user_profile["id"]),
        account_url=str(user_profile["html_url"]),
        scopes=token_bundle.scopes or settings.github_oauth_scopes,
        access_token_encrypted=encrypt_secret(token_bundle.access_token),
        refresh_token_encrypted=(
            encrypt_secret(token_bundle.refresh_token)
            if token_bundle.refresh_token is not None
            else None
        ),
        access_token_expires_at=token_bundle.expires_at,
    )
    store.save_repo_hosting_connection(connection)
    return RepoHostingConnectCallbackResponse(
        connection=RepoHostingConnectionInfo.from_connection(connection)
    )


async def list_github_repositories(user: User, store: JobStore) -> GitHubRepositoryListResponse:
    connection = store.get_repo_hosting_connection(user.id, RepoHostingProvider.GITHUB)
    if connection is None or connection.access_token_encrypted is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Connect your GitHub account before loading repositories.",
        )

    try:
        access_token = decrypt_secret(connection.access_token_encrypted, provider="github")
    except AppError as exc:
        # A stored token that can no longer be decrypted (e.g. the encryption
        # key rotated since it was saved) is functionally the same as never
        # having connected — surface the same actionable, user-safe message
        # instead of a bare INTERNAL error.
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Connect your GitHub account before loading repositories.",
            internal_detail=exc.internal_detail,
        ) from exc
    repos = await _fetch_user_repositories(access_token)
    return GitHubRepositoryListResponse(repos=repos)


def disconnect_github_connection(user: User, store: JobStore) -> None:
    if store.get_repo_hosting_connection(user.id, RepoHostingProvider.GITHUB) is None:
        raise AppError(
            ErrorCode.INPUT_INVALID,
            user_message="No GitHub connection is stored for this user.",
        )
    store.delete_repo_hosting_connection(user.id, RepoHostingProvider.GITHUB)


async def _exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    callback_url: str,
    code: str,
) -> GitHubTokenBundle:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                _TOKEN_URL,
                headers={"Accept": "application/json"},
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": callback_url,
                },
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"github token exchange returned {response.status_code}",
        )
    body = response.json()
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail="github token exchange missing access_token",
        )
    refresh_token = str(body.get("refresh_token") or "").strip() or None
    expires_in = body.get("expires_in")
    secrets.register_secret(access_token)
    if refresh_token is not None:
        secrets.register_secret(refresh_token)
    expires_at = None
    if expires_in:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if expires_at <= datetime.now(UTC) + timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS):
            expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
    scope_text = body.get("scope")
    scopes = [scope for scope in str(scope_text or "").split(",") if scope]
    return GitHubTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )


async def _fetch_current_user(access_token: str) -> dict[str, str | int]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                _USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
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
                "GitHub rejected the delegated sign-in response. Reconnect your GitHub account."
            ),
            internal_detail=f"github user lookup returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"github user lookup returned {response.status_code}",
        )
    body = response.json()
    login = str(body.get("login") or "").strip()
    html_url = str(body.get("html_url") or "").strip()
    account_id = body.get("id")
    if not login or not html_url or account_id is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail="github user payload missing login/html_url/id",
        )
    return {"login": login, "html_url": html_url, "id": int(account_id)}


async def _fetch_user_repositories(access_token: str) -> list[GitHubRepositorySummary]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                _REPOS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={
                    "affiliation": "owner,collaborator,organization_member",
                    "per_page": "100",
                    "sort": "updated",
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
                "GitHub rejected repository access for this connection. "
                "Reconnect your GitHub account."
            ),
            internal_detail=f"github repo listing returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"github repo listing returned {response.status_code}",
        )

    payload: list[dict[str, Any]] = response.json()
    repos: list[GitHubRepositorySummary] = []
    for item in payload:
        owner: dict[str, Any] = item.get("owner") or {}
        repos.append(
            GitHubRepositorySummary(
                id=int(item["id"]),
                name=str(item["name"]),
                full_name=str(item["full_name"]),
                html_url=str(item["html_url"]),
                clone_url=str(item["clone_url"]),
                default_branch=(
                    str(item["default_branch"]).strip() if item.get("default_branch") else None
                ),
                owner_login=str(owner["login"]),
                private=bool(item["private"]),
            )
        )
    return repos
