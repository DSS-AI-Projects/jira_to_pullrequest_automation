"""Repository hosting connection helpers for multi-user provider integrations."""

from __future__ import annotations

from app.auth.models import (
    RepoHostingConnectionInfo,
    RepoHostingProvider,
    RepoHostingProviderStatus,
    RepoHostingStatus,
    User,
)
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.store import JobStore

_PROVIDER_DISPLAY_NAMES: dict[RepoHostingProvider, str] = {
    RepoHostingProvider.GITHUB: "GitHub",
    RepoHostingProvider.GITLAB: "GitLab",
}


def _provider_enabled(provider: RepoHostingProvider, settings: Settings) -> bool:
    if provider == RepoHostingProvider.GITHUB:
        return settings.github_oauth_enabled
    return settings.gitlab_oauth_enabled


def _provider_configured(provider: RepoHostingProvider, settings: Settings) -> bool:
    if provider == RepoHostingProvider.GITHUB:
        return bool(
            settings.github_oauth_enabled
            and settings.github_oauth_client_id
            and settings.github_oauth_callback_url
            and settings.github_oauth_scopes
        )
    return bool(
        settings.gitlab_oauth_enabled
        and settings.gitlab_oauth_client_id
        and settings.gitlab_oauth_callback_url
    )


def get_repo_hosting_status(user: User, store: JobStore, settings: Settings) -> RepoHostingStatus:
    providers: list[RepoHostingProviderStatus] = []
    for provider in RepoHostingProvider:
        connection = store.get_repo_hosting_connection(user.id, provider)
        providers.append(
            RepoHostingProviderStatus(
                provider=provider,
                display_name=_PROVIDER_DISPLAY_NAMES[provider],
                enabled=_provider_enabled(provider, settings),
                configured=_provider_configured(provider, settings),
                connected=connection is not None,
                connection=(
                    RepoHostingConnectionInfo.from_connection(connection)
                    if connection is not None
                    else None
                ),
            )
        )
    return RepoHostingStatus(providers=providers)


def disconnect_repo_hosting_connection(
    user: User, provider: RepoHostingProvider, store: JobStore
) -> None:
    if store.get_repo_hosting_connection(user.id, provider) is None:
        raise AppError(
            ErrorCode.INPUT_INVALID,
            user_message=f"No { _PROVIDER_DISPLAY_NAMES[provider] } connection is stored for this user.",
        )
    store.delete_repo_hosting_connection(user.id, provider)
