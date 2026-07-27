"""Atlassian OAuth helpers for per-user Jira delegated access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from app.auth.models import (
    JiraAuthMode,
    JiraAuthStatus,
    JiraCloudSite,
    JiraConnectCallbackResponse,
    JiraConnection,
    JiraConnectionInfo,
    JiraConnectStartResponse,
    User,
)
from app.core import secrets
from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import AppError, ErrorCode
from app.jobs.store import JobStore

_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
_API_BASE = "https://api.atlassian.com/ex/jira"
_TOKEN_REFRESH_SKEW_SECONDS = 60


@dataclass(frozen=True)
class OAuthTokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str]


def _split_scopes(scope_text: str | None) -> list[str]:
    return [scope for scope in (scope_text or "").split() if scope]


def shared_jira_is_configured(settings: Settings) -> bool:
    return bool(settings.jira_base_url and settings.jira_email and secrets.get_jira_api_token())


def jira_oauth_is_configured(settings: Settings) -> bool:
    return bool(
        settings.jira_oauth_enabled
        and settings.jira_oauth_client_id
        and settings.jira_oauth_callback_url
        and secrets.get_jira_oauth_client_secret()
        and secrets.get_jira_oauth_encryption_key()
    )


def _require_oauth_configured(settings: Settings) -> tuple[str, str]:
    client_id = settings.jira_oauth_client_id
    callback_url = settings.jira_oauth_callback_url
    client_secret = secrets.get_jira_oauth_client_secret()
    encryption_key = secrets.get_jira_oauth_encryption_key()
    if client_id and callback_url and client_secret and encryption_key:
        return client_id, callback_url

    missing: list[str] = []
    if not settings.jira_oauth_enabled:
        missing.append("JIRA_OAUTH_ENABLED")
    if not client_id:
        missing.append("JIRA_OAUTH_CLIENT_ID")
    if not callback_url:
        missing.append("JIRA_OAUTH_CALLBACK_URL")
    if not client_secret:
        missing.append("JIRA_OAUTH_CLIENT_SECRET")
    if not encryption_key:
        missing.append("JIRA_OAUTH_ENCRYPTION_KEY")
    raise AppError(
        ErrorCode.JIRA_OAUTH_NOT_AVAILABLE,
        internal_detail="Missing Jira OAuth configuration: " + ", ".join(missing),
    )


def get_jira_auth_status(user: User, store: JobStore, settings: Settings) -> JiraAuthStatus:
    connection = store.get_jira_connection(user.id)
    shared_configured = shared_jira_is_configured(settings)
    oauth_configured = jira_oauth_is_configured(settings)
    effective_mode = JiraAuthMode.UNCONFIGURED
    if connection is not None:
        effective_mode = JiraAuthMode.DELEGATED
    elif shared_configured:
        effective_mode = JiraAuthMode.SHARED
    return JiraAuthStatus(
        oauth_enabled=settings.jira_oauth_enabled,
        oauth_configured=oauth_configured,
        shared_configured=shared_configured,
        effective_mode=effective_mode,
        connected=connection is not None,
        connection=(
            JiraConnectionInfo.from_connection(connection) if connection is not None else None
        ),
    )


def create_authorization_request(
    user: User, store: JobStore, settings: Settings
) -> JiraConnectStartResponse:
    client_id, callback_url = _require_oauth_configured(settings)
    oauth_state = store.create_jira_oauth_state(user.id, settings.jira_oauth_state_ttl_minutes)
    params = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": " ".join(settings.jira_oauth_scopes),
            "redirect_uri": callback_url,
            "state": oauth_state.state,
            "response_type": "code",
            "prompt": "consent",
        }
    )
    return JiraConnectStartResponse(authorization_url=f"{_AUTHORIZE_URL}?{params}")


async def complete_authorization(
    *,
    user: User,
    store: JobStore,
    settings: Settings,
    code: str,
    state: str,
) -> JiraConnectCallbackResponse:
    client_id, callback_url = _require_oauth_configured(settings)
    if not store.consume_jira_oauth_state(state, user.id):
        raise AppError(ErrorCode.JIRA_OAUTH_STATE_INVALID)
    token_bundle = await _exchange_authorization_code(
        client_id=client_id,
        client_secret=secrets.get_jira_oauth_client_secret() or "",
        callback_url=callback_url,
        code=code,
    )
    site = await _resolve_accessible_site(token_bundle.access_token, settings)
    scopes = token_bundle.scopes or settings.jira_oauth_scopes
    connection = JiraConnection.new(
        user_id=user.id,
        site=site,
        scopes=scopes,
        access_token_encrypted=encrypt_secret(token_bundle.access_token),
        refresh_token_encrypted=(
            encrypt_secret(token_bundle.refresh_token)
            if token_bundle.refresh_token is not None
            else None
        ),
        access_token_expires_at=token_bundle.expires_at,
    )
    store.save_jira_connection(connection)
    return JiraConnectCallbackResponse(connection=JiraConnectionInfo.from_connection(connection))


def disconnect_connection(user: User, store: JobStore) -> None:
    store.delete_jira_connection(user.id)


async def get_user_access_token(
    user_id: str, store: JobStore, settings: Settings
) -> tuple[JiraConnection, str] | None:
    connection = store.get_jira_connection(user_id)
    if connection is None:
        return None
    if _is_token_stale(connection.access_token_expires_at):
        connection = await _refresh_connection(connection, store, settings)
    return connection, decrypt_secret(connection.access_token_encrypted)


def build_delegated_api_base(cloud_id: str) -> str:
    return f"{_API_BASE}/{cloud_id}"


def _is_token_stale(expires_at: datetime) -> bool:
    now = datetime.now(UTC) + timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS)
    return expires_at <= now


async def _exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    callback_url: str,
    code: str,
) -> OAuthTokenBundle:
    return await _exchange_token(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": callback_url,
        }
    )


async def _refresh_connection(
    connection: JiraConnection, store: JobStore, settings: Settings
) -> JiraConnection:
    client_id, _callback_url = _require_oauth_configured(settings)
    client_secret = secrets.get_jira_oauth_client_secret() or ""
    if connection.refresh_token_encrypted is None:
        raise AppError(
            ErrorCode.JIRA_AUTH_FAILED,
            user_message="Your Jira connection has expired. Reconnect your Jira account.",
            internal_detail=f"missing refresh token for user {connection.user_id}",
        )
    token_bundle = await _exchange_token(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": decrypt_secret(connection.refresh_token_encrypted),
        }
    )
    connection.access_token_encrypted = encrypt_secret(token_bundle.access_token)
    if token_bundle.refresh_token is not None:
        connection.refresh_token_encrypted = encrypt_secret(token_bundle.refresh_token)
    connection.access_token_expires_at = token_bundle.expires_at
    store.save_jira_connection(connection)
    return connection


async def _exchange_token(payload: dict[str, str]) -> OAuthTokenBundle:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(_TOKEN_URL, json=payload)
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.JIRA_UNREACHABLE,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code != 200:
        raise AppError(
            ErrorCode.JIRA_OAUTH_CALLBACK_FAILED,
            internal_detail=f"token exchange returned {response.status_code}",
        )

    body = response.json()
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise AppError(
            ErrorCode.JIRA_OAUTH_CALLBACK_FAILED,
            internal_detail="token exchange response missing access_token",
        )
    refresh_token = str(body.get("refresh_token") or "").strip() or None
    expires_in = int(body.get("expires_in") or 3600)
    secrets.register_secret(access_token)
    if refresh_token is not None:
        secrets.register_secret(refresh_token)
    return OAuthTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=_split_scopes(body.get("scope") if isinstance(body.get("scope"), str) else None),
    )


async def _resolve_accessible_site(access_token: str, settings: Settings) -> JiraCloudSite:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                _ACCESSIBLE_RESOURCES_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.JIRA_UNREACHABLE,
            internal_detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if response.status_code in (401, 403):
        raise AppError(
            ErrorCode.JIRA_AUTH_FAILED,
            user_message=(
                "Jira rejected the delegated sign-in response. Reconnect your Jira account."
            ),
            internal_detail=f"accessible-resources returned {response.status_code}",
        )
    if response.status_code != 200:
        raise AppError(
            ErrorCode.JIRA_OAUTH_CALLBACK_FAILED,
            internal_detail=f"accessible-resources returned {response.status_code}",
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise AppError(
            ErrorCode.JIRA_OAUTH_CALLBACK_FAILED,
            internal_detail="accessible-resources response was not a list",
        )
    resources: list[JiraCloudSite] = []
    for raw in cast(list[Any], payload):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        if not (item.get("id") and item.get("url")):
            continue
        resources.append(
            JiraCloudSite(
                id=str(item.get("id") or "").strip(),
                name=str(item.get("name") or "").strip() or "Jira Cloud",
                url=str(item.get("url") or "").strip(),
            )
        )
    if not resources:
        raise AppError(
            ErrorCode.JIRA_SITE_NOT_ACCESSIBLE,
            user_message="Your Jira account does not expose any Jira Cloud sites to this app.",
        )
    if settings.jira_base_url:
        target = settings.jira_base_url.rstrip("/").lower()
        for resource in resources:
            if resource.url.rstrip("/").lower() == target:
                return resource
        raise AppError(
            ErrorCode.JIRA_SITE_NOT_ACCESSIBLE,
            internal_detail=(
                f"Configured site {settings.jira_base_url} not in accessible resources"
            ),
        )
    if len(resources) == 1:
        return resources[0]
    raise AppError(
        ErrorCode.JIRA_SITE_NOT_ACCESSIBLE,
        user_message=(
            "Your Jira account can access multiple Jira sites. Configure JIRA_BASE_URL on the "
            "server, then reconnect your Jira account."
        ),
    )
