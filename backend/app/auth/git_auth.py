"""Git-over-HTTPS auth as the signed-in user, for `git clone` and `git push`.

One place for the whole policy, so clone and push can't drift apart:

- **Which provider a URL belongs to**, by exact host: github.com → GitHub,
  the configured `GITLAB_INSTANCE_URL` host → GitLab, bitbucket.org →
  Bitbucket. A token is only ever offered to its own provider's host.
- **Which granted scopes allow which operation** — read (clone) vs write
  (push). A token whose scopes don't cover the operation is never tried:
  e.g. a GitLab connection made before `read_repository`/`write_repository`
  were requested gets a 401 from GitLab's git endpoint however it's
  presented, and on Windows that 401 then sends git into Git Credential
  Manager's fallback, producing unrelated-looking errors — checking the
  scopes on file first gives an actionable "reconnect" instead.
- **How the token is presented**: HTTP Basic with the provider's fixed
  username, as a one-off `git -c http.extraHeader=...` for that single git
  process — never in the URL, so never persisted into a workspace's
  `.git/config` where the agents could read it (invariant 3). Bearer is not
  an option: GitLab's git endpoint rejects it (verified live).

Clone is best-effort (`delegated_clone_auth_header` → None means "use the
machine's ambient git auth"); push in delegated mode is strict
(`resolve_push_auth` raises a typed, user-actionable error instead of ever
falling back to machine credentials).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from app.auth.bitbucket_oauth import (
    BITBUCKET_HOST,
    bitbucket_oauth_is_configured,
    fresh_bitbucket_access_token,
)
from app.auth.github_oauth import fresh_github_access_token, github_oauth_is_configured
from app.auth.gitlab_oauth import fresh_gitlab_access_token, gitlab_oauth_is_configured
from app.auth.models import RepoHostingConnection, RepoHostingProvider, User
from app.core import secrets
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.store import JobStore

GitAccess = Literal["read", "write"]

GITHUB_HOST = "github.com"

_DISPLAY_NAMES = {
    RepoHostingProvider.GITHUB: "GitHub",
    RepoHostingProvider.GITLAB: "GitLab",
    RepoHostingProvider.BITBUCKET: "Bitbucket",
}

# The fixed Basic-auth username each provider's git endpoint expects with an
# OAuth access token. GitLab `oauth2` and Bitbucket `x-token-auth` are
# verified live; GitHub accepts any username with a token as the password.
_BASIC_USERNAMES = {
    RepoHostingProvider.GITHUB: "x-access-token",
    RepoHostingProvider.GITLAB: "oauth2",
    RepoHostingProvider.BITBUCKET: "x-token-auth",
}

# Granted scopes that permit each operation. Supersets count: GitLab `api`
# includes repository access; Bitbucket documents `pullrequest` as implying
# repository read and `pullrequest:write` as implying repository write.
_SCOPES: dict[RepoHostingProvider, dict[GitAccess, frozenset[str]]] = {
    RepoHostingProvider.GITHUB: {
        "read": frozenset({"repo", "public_repo"}),
        "write": frozenset({"repo", "public_repo"}),
    },
    RepoHostingProvider.GITLAB: {
        "read": frozenset({"read_repository", "write_repository", "api"}),
        "write": frozenset({"write_repository", "api"}),
    },
    RepoHostingProvider.BITBUCKET: {
        "read": frozenset(
            {
                "repository",
                "repository:write",
                "repository:admin",
                "pullrequest",
                "pullrequest:write",
            }
        ),
        "write": frozenset({"repository:write", "repository:admin", "pullrequest:write"}),
    },
}

# What a user must do to get a write-capable connection, per provider.
_WRITE_REMEDY = {
    RepoHostingProvider.GITHUB: "Reconnect your GitHub account (it needs the `repo` scope).",
    RepoHostingProvider.GITLAB: (
        "Reconnect your GitLab account to grant write access (`write_repository`)."
    ),
    RepoHostingProvider.BITBUCKET: (
        "Reconnect your Bitbucket account — the Bitbucket OAuth consumer must allow "
        "Repositories: Write."
    ),
}


def provider_display_name(provider: RepoHostingProvider) -> str:
    return _DISPLAY_NAMES[provider]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_SCP_LIKE_RE = re.compile(r"^(?:[^@/\s]+@)?([A-Za-z0-9.-]{2,}):(?!//)(.+)$")


def https_remote_url(url: str) -> str:
    """The https form of a remote URL. A LOCAL job's `origin_url` is often an
    SSH remote (`git@github.com:org/repo.git`, `ssh://git@host/path`), but a
    token can only be presented over HTTPS, so delegated push targets the
    same repository by its https URL. Anything else is returned unchanged."""
    if url.startswith("ssh://"):
        parts = urlsplit(url)
        return f"https://{parts.hostname}{parts.path}" if parts.hostname else url
    match = _SCP_LIKE_RE.match(url)
    if match and "://" not in url:
        return f"https://{match.group(1)}/{match.group(2).lstrip('/')}"
    return url


def provider_for_url(url: str, settings: Settings) -> RepoHostingProvider | None:
    host = _host(url)
    if not host:
        return None
    if host == GITHUB_HOST:
        return RepoHostingProvider.GITHUB
    if host == _host(settings.gitlab_instance_url):
        return RepoHostingProvider.GITLAB
    if host == BITBUCKET_HOST:
        return RepoHostingProvider.BITBUCKET
    return None


def is_github_app_connection(connection: RepoHostingConnection) -> bool:
    """A GitHub App user-to-server token is granted no OAuth scopes at all
    (the token response has `scope: ""`); an OAuth App token always reports
    the scopes it was granted. See github_oauth.py's module docstring."""
    return connection.provider == RepoHostingProvider.GITHUB and not connection.scopes


# Shown before a GitHub App push, since the app's own permission and
# installation — not anything on the connection — decide whether it works.
GITHUB_APP_PUSH_NOTE = (
    "This GitHub connection is a GitHub App, so pushing also needs the app to be "
    "installed on this repository's account or organization with the Contents: "
    "Read and write permission."
)


def has_scope_for(connection: RepoHostingConnection, access: GitAccess) -> bool:
    if is_github_app_connection(connection):
        # Access is the app's permissions × its installations × the user's own
        # access — unknowable from the connection, so let GitHub decide and
        # say so up front (GITHUB_APP_PUSH_NOTE) rather than block a push
        # that may well be allowed.
        return True
    return not _SCOPES[connection.provider][access].isdisjoint(connection.scopes)


def provider_is_configured(provider: RepoHostingProvider, settings: Settings) -> bool:
    match provider:
        case RepoHostingProvider.GITHUB:
            return github_oauth_is_configured(settings)
        case RepoHostingProvider.GITLAB:
            return gitlab_oauth_is_configured(settings)
        case RepoHostingProvider.BITBUCKET:
            return bitbucket_oauth_is_configured(settings)


async def _fresh_token(
    connection: RepoHostingConnection, store: JobStore, settings: Settings
) -> str:
    match connection.provider:
        case RepoHostingProvider.GITHUB:
            return await fresh_github_access_token(connection, store, settings)
        case RepoHostingProvider.GITLAB:
            return await fresh_gitlab_access_token(connection, store, settings)
        case RepoHostingProvider.BITBUCKET:
            return await fresh_bitbucket_access_token(connection, store, settings)


def git_auth_header(provider: RepoHostingProvider, access_token: str) -> str:
    """`Authorization: Basic <base64(username:token)>` for one git process."""
    raw = f"{_BASIC_USERNAMES[provider]}:{access_token}".encode()
    encoded = base64.b64encode(raw).decode("ascii")
    # The encoded form is a distinct string from the raw token, so the log
    # redactor wouldn't recognize it as a secret unless registered too.
    secrets.register_secret(encoded)
    return f"Authorization: Basic {encoded}"


async def delegated_clone_auth_header(
    repo_url: str, user_id: str, store: JobStore, settings: Settings
) -> str | None:
    """Best-effort read auth for `git clone` as the job owner, or None to use
    the machine's ambient git auth — never raises."""
    provider = provider_for_url(repo_url, settings)
    if provider is None or not provider_is_configured(provider, settings):
        return None
    connection = store.get_repo_hosting_connection(user_id, provider)
    if connection is None or connection.access_token_encrypted is None:
        return None
    if not has_scope_for(connection, "read"):
        return None
    try:
        token = await _fresh_token(connection, store, settings)
    except AppError:
        return None
    return git_auth_header(provider, token)


@dataclass(frozen=True)
class PushIdentity:
    """Who a delegated push would run as, or why it can't — computed without
    touching git, so the job page can show it before the user clicks Push."""

    provider: RepoHostingProvider | None
    account_name: str | None
    ready: bool
    reason: str | None
    # A caveat for a ready push the provider may still refuse (GitHub App).
    note: str | None = None


def _not_ready(provider: RepoHostingProvider | None, reason: str) -> PushIdentity:
    return PushIdentity(provider=provider, account_name=None, ready=False, reason=reason)


def describe_push_identity(
    remote_url: str | None, user: User | None, store: JobStore, settings: Settings
) -> PushIdentity:
    if not remote_url:
        return _not_ready(None, "This job's repository has no remote to push to.")
    if user is None:
        return _not_ready(
            None, "Pushing as yourself needs app sign-in (AUTH_ENABLED) to be turned on."
        )
    remote_url = https_remote_url(remote_url)
    if not remote_url.startswith("https://"):
        # Never send a token over plain http (or to an unrecognized scheme).
        return _not_ready(None, "Pushing as yourself needs an https remote URL.")
    provider = provider_for_url(remote_url, settings)
    if provider is None:
        host = _host(remote_url) or remote_url
        return _not_ready(
            None,
            f"Pushing to {host} isn't supported — only GitHub, the configured GitLab "
            "instance, and Bitbucket Cloud can push as the signed-in user.",
        )
    name = provider_display_name(provider)
    if not provider_is_configured(provider, settings):
        return _not_ready(provider, f"{name} sign-in isn't configured on this server.")
    connection = store.get_repo_hosting_connection(user.id, provider)
    if connection is None or connection.access_token_encrypted is None:
        return _not_ready(provider, f"Connect your {name} account to push to this repository.")
    if not has_scope_for(connection, "write"):
        return PushIdentity(
            provider=provider,
            account_name=connection.account_name,
            ready=False,
            reason=f"Your {name} connection is read-only. {_WRITE_REMEDY[provider]}",
        )
    return PushIdentity(
        provider=provider,
        account_name=connection.account_name,
        ready=True,
        reason=None,
        note=GITHUB_APP_PUSH_NOTE if is_github_app_connection(connection) else None,
    )


@dataclass(frozen=True)
class PushAuth:
    header: str
    provider: RepoHostingProvider
    account_name: str
    remote_url: str  # the https URL the header is valid for


async def resolve_push_auth(
    remote_url: str, user: User | None, store: JobStore, settings: Settings
) -> PushAuth:
    """The auth header for pushing as `user` (whoever clicked Push), or a
    typed error saying exactly what to fix. Never falls back to the
    machine's own git credentials."""
    identity = describe_push_identity(remote_url, user, store, settings)
    if not identity.ready or identity.provider is None or user is None:
        code = (
            ErrorCode.BRANCH_PUSH_REAUTH_REQUIRED
            if identity.provider is not None
            else ErrorCode.BRANCH_PUSH_NOT_AVAILABLE
        )
        raise AppError(code, user_message=identity.reason)
    connection = store.get_repo_hosting_connection(user.id, identity.provider)
    assert connection is not None  # describe_push_identity just checked it
    name = provider_display_name(identity.provider)
    try:
        token = await _fresh_token(connection, store, settings)
    except AppError as exc:
        raise AppError(
            ErrorCode.BRANCH_PUSH_REAUTH_REQUIRED,
            user_message=f"Your {name} connection has expired. Reconnect your {name} account.",
            internal_detail=exc.internal_detail,
        ) from exc
    return PushAuth(
        header=git_auth_header(identity.provider, token),
        provider=identity.provider,
        account_name=connection.account_name,
        remote_url=https_remote_url(remote_url),
    )
