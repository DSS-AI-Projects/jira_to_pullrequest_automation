"""Bitbucket Cloud OAuth helpers for per-user repository provider connections.

Mirrors gitlab_oauth.py's shape (short-lived access tokens plus refresh, the
same stale-token/refresh-before-use pattern), with Bitbucket Cloud's own
protocol differences rather than a re-skin:

1. **bitbucket.org only.** Bitbucket Cloud is a single SaaS host, so the
   OAuth and API base URLs are constants — unlike GitLab, there is no
   instance URL to configure. Bitbucket Data Center/Server uses a different
   API and is not supported by this module.
2. **Client credentials go in an HTTP Basic header** on the token endpoint
   (`client_id:client_secret`), with a form-encoded body — not in a JSON body
   the way GitLab accepts them.
3. **The consumer, not the request, decides the grant.** A Bitbucket OAuth
   consumer's permissions are ticked on the consumer itself (workspace
   settings → OAuth consumers); the token response reports what was actually
   granted in a space-separated `scopes` field (plural, unlike GitLab's
   `scope`), which is what gets stored on the connection.
4. **Clone links carry a username** (`https://someone@bitbucket.org/ws/repo.git`);
   the repo list strips that userinfo so the URL a job stores is the plain
   `https://bitbucket.org/<workspace>/<repo>.git`.
5. **Repos are listed per workspace.** Bitbucket removed its cross-workspace
   `GET /2.0/repositories` listing; the picker lists the user's workspaces
   (`/2.0/user/workspaces`) and then each one's repositories.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.auth.models import (
    BitbucketRepositoryListResponse,
    BitbucketRepositorySummary,
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

BITBUCKET_HOST = "bitbucket.org"
_AUTHORIZE_URL = "https://bitbucket.org/site/oauth2/authorize"
_TOKEN_URL = "https://bitbucket.org/site/oauth2/access_token"
_API_BASE_URL = "https://api.bitbucket.org/2.0"
_TOKEN_REFRESH_SKEW_SECONDS = 60
_REPO_PAGE_SIZE = 100  # Bitbucket's own maximum `pagelen` for this endpoint
_MAX_WORKSPACES = 25  # one repo-listing request per workspace, so keep it bounded


@dataclass(frozen=True)
class BitbucketTokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


def bitbucket_oauth_is_configured(settings: Settings) -> bool:
    return bool(
        settings.bitbucket_oauth_enabled
        and settings.bitbucket_oauth_client_id
        and settings.bitbucket_oauth_callback_url
        and secrets.get_bitbucket_oauth_client_secret()
        and secrets.get_bitbucket_oauth_encryption_key()
    )


def _require_bitbucket_oauth_configured(settings: Settings) -> tuple[str, str]:
    client_id = settings.bitbucket_oauth_client_id
    callback_url = settings.bitbucket_oauth_callback_url
    client_secret = secrets.get_bitbucket_oauth_client_secret()
    encryption_key = secrets.get_bitbucket_oauth_encryption_key()
    if client_id and callback_url and client_secret and encryption_key:
        return client_id, client_secret

    missing: list[str] = []
    if not settings.bitbucket_oauth_enabled:
        missing.append("BITBUCKET_OAUTH_ENABLED")
    if not client_id:
        missing.append("BITBUCKET_OAUTH_CLIENT_ID")
    if not callback_url:
        missing.append("BITBUCKET_OAUTH_CALLBACK_URL")
    if not client_secret:
        missing.append("BITBUCKET_OAUTH_CLIENT_SECRET")
    if not encryption_key:
        missing.append("BITBUCKET_OAUTH_ENCRYPTION_KEY")
    raise AppError(
        ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
        internal_detail="Missing Bitbucket OAuth configuration: " + ", ".join(missing),
    )


def create_bitbucket_authorization_request(
    user: User, store: JobStore, settings: Settings
) -> RepoHostingConnectStartResponse:
    client_id, _client_secret = _require_bitbucket_oauth_configured(settings)
    oauth_state = store.create_provider_oauth_state(
        user.id, RepoHostingProvider.BITBUCKET, settings.bitbucket_oauth_state_ttl_minutes
    )
    # No redirect_uri: Bitbucket always redirects to the callback URL
    # registered on the consumer, and a redirect_uri sent here would then
    # also have to be repeated on the token exchange. BITBUCKET_OAUTH_CALLBACK_URL
    # must match that registered URL (it's where this app's callback page lives).
    params = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "state": oauth_state.state,
        }
    )
    return RepoHostingConnectStartResponse(authorization_url=f"{_AUTHORIZE_URL}?{params}")


async def complete_bitbucket_authorization(
    *,
    user: User,
    store: JobStore,
    settings: Settings,
    code: str,
    state: str,
) -> RepoHostingConnectCallbackResponse:
    client_id, client_secret = _require_bitbucket_oauth_configured(settings)
    if not store.consume_provider_oauth_state(state, user.id, RepoHostingProvider.BITBUCKET):
        raise AppError(ErrorCode.REPO_PROVIDER_STATE_INVALID)
    token_bundle = await _exchange_token(
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "code": code},
    )
    user_profile = await _fetch_current_user(token_bundle.access_token)
    connection = RepoHostingConnection.new(
        user_id=user.id,
        provider=RepoHostingProvider.BITBUCKET,
        auth_kind=RepoHostingAuthKind.OAUTH_USER,
        account_name=user_profile["username"],
        account_id=user_profile["id"],
        account_url=user_profile["web_url"],
        scopes=token_bundle.scopes or settings.bitbucket_oauth_scopes,
        access_token_encrypted=encrypt_secret(token_bundle.access_token, provider="bitbucket"),
        refresh_token_encrypted=(
            encrypt_secret(token_bundle.refresh_token, provider="bitbucket")
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
        # Bitbucket always returns expires_in (2h), so a connection this
        # module created always has an expiry — never treat "unknown" as fresh.
        return True
    now = datetime.now(UTC) + timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS)
    return expires_at <= now


async def _get_valid_access_token(
    connection: RepoHostingConnection, store: JobStore, settings: Settings
) -> tuple[RepoHostingConnection, str]:
    if _is_token_stale(connection.access_token_expires_at):
        connection = await _refresh_connection(connection, store, settings)
    access_token = decrypt_secret(connection.access_token_encrypted or "", provider="bitbucket")
    return connection, access_token


async def _refresh_connection(
    connection: RepoHostingConnection, store: JobStore, settings: Settings
) -> RepoHostingConnection:
    client_id, client_secret = _require_bitbucket_oauth_configured(settings)
    if connection.refresh_token_encrypted is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Your Bitbucket connection has expired. Reconnect your Bitbucket account.",
            internal_detail=f"missing refresh token for user {connection.user_id}",
        )
    token_bundle = await _exchange_token(
        client_id,
        client_secret,
        {
            "grant_type": "refresh_token",
            "refresh_token": decrypt_secret(
                connection.refresh_token_encrypted, provider="bitbucket"
            ),
        },
    )
    connection.access_token_encrypted = encrypt_secret(
        token_bundle.access_token, provider="bitbucket"
    )
    if token_bundle.refresh_token is not None:
        connection.refresh_token_encrypted = encrypt_secret(
            token_bundle.refresh_token, provider="bitbucket"
        )
    connection.access_token_expires_at = token_bundle.expires_at
    store.save_repo_hosting_connection(connection)
    return connection


async def list_bitbucket_repositories(
    user: User, store: JobStore, settings: Settings
) -> BitbucketRepositoryListResponse:
    connection = store.get_repo_hosting_connection(user.id, RepoHostingProvider.BITBUCKET)
    if connection is None or connection.access_token_encrypted is None:
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Connect your Bitbucket account before loading repositories.",
        )

    try:
        _connection, access_token = await _get_valid_access_token(connection, store, settings)
    except AppError as exc:
        # Same reasoning as gitlab_oauth: an undecryptable or unrefreshable
        # stored token is functionally "not connected" — say so actionably.
        raise AppError(
            ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
            user_message="Connect your Bitbucket account before loading repositories.",
            internal_detail=exc.internal_detail,
        ) from exc
    repos = await _fetch_user_repositories(access_token)
    return BitbucketRepositoryListResponse(repos=repos)


async def fresh_bitbucket_access_token(
    connection: RepoHostingConnection, store: JobStore, settings: Settings
) -> str:
    """The decrypted access token for a stored Bitbucket connection,
    refreshed first if it's stale. Raises AppError when it can't be
    decrypted or refreshed. Scope policy lives in app/auth/git_auth.py."""
    _connection, access_token = await _get_valid_access_token(connection, store, settings)
    secrets.register_secret(access_token)
    return access_token


async def _exchange_token(
    client_id: str, client_secret: str, form: dict[str, str]
) -> BitbucketTokenBundle:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                _TOKEN_URL,
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
                data=form,
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"bitbucket token exchange returned {response.status_code}",
        )
    body = response.json()
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail="bitbucket token exchange missing access_token",
        )
    refresh_token = str(body.get("refresh_token") or "").strip() or None
    expires_in = body.get("expires_in")
    secrets.register_secret(access_token)
    if refresh_token is not None:
        secrets.register_secret(refresh_token)
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
    scope_text = body.get("scopes") or body.get("scope")
    scopes = [scope for scope in str(scope_text or "").split(" ") if scope]
    return BitbucketTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )


async def _fetch_current_user(access_token: str) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{_API_BASE_URL}/user",
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
                "Bitbucket rejected the delegated sign-in response. "
                "Reconnect your Bitbucket account."
            ),
            internal_detail=f"bitbucket user lookup returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"bitbucket user lookup returned {response.status_code}",
        )
    body = response.json()
    # `username` is the stable handle; `nickname`/`display_name` are the
    # fallbacks Bitbucket documents for accounts whose username is hidden.
    username = str(
        body.get("username") or body.get("nickname") or body.get("display_name") or ""
    ).strip()
    account_id = str(body.get("uuid") or body.get("account_id") or "").strip()
    links: dict[str, Any] = body.get("links") or {}
    html_link: dict[str, Any] = links.get("html") or {}
    web_url = str(html_link.get("href") or "").strip()
    if not username or not account_id:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail="bitbucket user payload missing username/uuid",
        )
    return {
        "username": username,
        "id": account_id,
        "web_url": web_url or f"https://{BITBUCKET_HOST}/{username}/",
    }


def _plain_clone_url(full_name: str) -> str:
    # Bitbucket's own https clone link embeds the viewer's username
    # (https://someone@bitbucket.org/...). Rebuild it without userinfo so the
    # stored job repo URL is identity-free and passes repo-URL validation.
    return f"https://{BITBUCKET_HOST}/{full_name}.git"


async def _fetch_user_workspace_slugs(client: httpx.AsyncClient, access_token: str) -> list[str]:
    try:
        response = await client.get(
            f"{_API_BASE_URL}/user/workspaces",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"pagelen": str(_MAX_WORKSPACES)},
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
                "Bitbucket rejected repository access for this connection. "
                "Reconnect your Bitbucket account."
            ),
            internal_detail=f"bitbucket workspace listing returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"bitbucket workspace listing returned {response.status_code}",
        )
    body: dict[str, Any] = response.json()
    values: list[dict[str, Any]] = body.get("values") or []
    slugs: list[str] = []
    for item in values:
        workspace: dict[str, Any] = item.get("workspace") or {}
        slug = str(workspace.get("slug") or "").strip()
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs[:_MAX_WORKSPACES]


async def _fetch_workspace_repositories(
    client: httpx.AsyncClient, access_token: str, workspace_slug: str
) -> list[dict[str, Any]]:
    # Best-effort per workspace: one workspace the user can't list (or a
    # transient error) must not hide the repos of every other workspace.
    try:
        response = await client.get(
            f"{_API_BASE_URL}/repositories/{quote(workspace_slug, safe='')}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "role": "member",
                "pagelen": str(_REPO_PAGE_SIZE),
                "sort": "-updated_on",
            },
        )
    except httpx.HTTPError:
        return []
    if response.status_code != 200:
        return []
    body: dict[str, Any] = response.json()
    return body.get("values") or []


async def _fetch_user_repositories(access_token: str) -> list[BitbucketRepositorySummary]:
    # Bitbucket removed its cross-workspace listing endpoints (a bare
    # `GET /2.0/repositories` now 404s with "There is no API hosted at this
    # URL", verified against the live API), so listing is two-step: the
    # user's workspaces, then each workspace's repositories.
    async with httpx.AsyncClient(timeout=30) as client:
        slugs = await _fetch_user_workspace_slugs(client, access_token)
        per_workspace = await asyncio.gather(
            *(_fetch_workspace_repositories(client, access_token, slug) for slug in slugs)
        )

    # Merged, most recently updated first, capped to the same quick-pick size
    # GitLab's picker uses — a quick-pick list, not a full browser.
    # `updated_on` is ISO-8601, so string order is chronological order.
    values = sorted(
        (item for items in per_workspace for item in items),
        key=lambda item: str(item.get("updated_on") or ""),
        reverse=True,
    )[:_REPO_PAGE_SIZE]
    repos: list[BitbucketRepositorySummary] = []
    for item in values:
        full_name = str(item.get("full_name") or "").strip()
        if not full_name:
            continue
        links: dict[str, Any] = item.get("links") or {}
        html_link: dict[str, Any] = links.get("html") or {}
        mainbranch: dict[str, Any] = item.get("mainbranch") or {}
        workspace: dict[str, Any] = item.get("workspace") or {}
        repos.append(
            BitbucketRepositorySummary(
                uuid=str(item.get("uuid") or full_name),
                name=str(item.get("name") or full_name.split("/")[-1]),
                full_name=full_name,
                web_url=str(html_link.get("href") or f"https://{BITBUCKET_HOST}/{full_name}"),
                clone_url=_plain_clone_url(full_name),
                default_branch=(
                    str(mainbranch["name"]).strip() if mainbranch.get("name") else None
                ),
                workspace=str(workspace.get("slug") or full_name.split("/")[0]),
                private=bool(item.get("is_private", True)),
            )
        )
    return repos
