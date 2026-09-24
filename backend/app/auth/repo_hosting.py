"""Repository hosting connection helpers for multi-user provider integrations."""

from __future__ import annotations

from app.auth.bitbucket_oauth import bitbucket_oauth_is_configured
from app.auth.github_oauth import github_oauth_is_configured
from app.auth.gitlab_oauth import gitlab_oauth_is_configured
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
    RepoHostingProvider.BITBUCKET: "Bitbucket",
}


# Exhaustive `match` rather than if/else: with a third provider, an `else`
# fallback silently reported one provider's settings as another's — pyright
# flags a missing case here instead.
def _provider_enabled(provider: RepoHostingProvider, settings: Settings) -> bool:
    match provider:
        case RepoHostingProvider.GITHUB:
            return settings.github_oauth_enabled
        case RepoHostingProvider.GITLAB:
            return settings.gitlab_oauth_enabled
        case RepoHostingProvider.BITBUCKET:
            return settings.bitbucket_oauth_enabled


def _provider_configured(provider: RepoHostingProvider, settings: Settings) -> bool:
    # Delegates to each module's own is_configured() check — the single
    # source of truth for "can this provider's flow actually run", since it
    # also checks the client secret and encryption key (secrets.py), not
    # just the non-secret Settings fields checked here before GitLab's flow
    # existed.
    match provider:
        case RepoHostingProvider.GITHUB:
            return github_oauth_is_configured(settings)
        case RepoHostingProvider.GITLAB:
            return gitlab_oauth_is_configured(settings)
        case RepoHostingProvider.BITBUCKET:
            return bitbucket_oauth_is_configured(settings)


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
            user_message=(
                f"No {_PROVIDER_DISPLAY_NAMES[provider]} connection is stored for this user."
            ),
        )
    store.delete_repo_hosting_connection(user.id, provider)
