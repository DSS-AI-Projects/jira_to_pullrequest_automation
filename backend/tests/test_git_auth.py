"""Git-over-HTTPS auth as the signed-in user (app/auth/git_auth.py): which
provider a URL belongs to, which scopes allow clone vs push, the Basic header
form each provider's git endpoint accepts, and every "why can't I push yet"
reason a delegated push can hit."""

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.auth.git_auth import (
    delegated_clone_auth_header,
    describe_push_identity,
    git_auth_header,
    has_scope_for,
    https_remote_url,
    provider_for_url,
    resolve_push_auth,
)
from app.auth.models import (
    RepoHostingAuthKind,
    RepoHostingConnection,
    RepoHostingProvider,
    User,
)
from app.core.config import Settings, get_settings
from app.core.crypto import encrypt_secret
from app.core.errors import AppError, ErrorCode
from app.jobs.store import JobStore

GITHUB = RepoHostingProvider.GITHUB
GITLAB = RepoHostingProvider.GITLAB
BITBUCKET = RepoHostingProvider.BITBUCKET

_ENV_PREFIX = {GITHUB: "GITHUB", GITLAB: "GITLAB", BITBUCKET: "BITBUCKET"}
_CRYPTO_NAME = {GITHUB: "github", GITLAB: "gitlab", BITBUCKET: "bitbucket"}


@pytest.fixture
def providers_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    for prefix in _ENV_PREFIX.values():
        monkeypatch.setenv(f"{prefix}_OAUTH_ENABLED", "true")
        monkeypatch.setenv(f"{prefix}_OAUTH_CLIENT_ID", f"{prefix.lower()}-client")
        monkeypatch.setenv(f"{prefix}_OAUTH_CALLBACK_URL", "http://testserver/callback")
        monkeypatch.setenv(f"{prefix}_OAUTH_CLIENT_SECRET", f"{prefix.lower()}-secret")
        monkeypatch.setenv(f"{prefix}_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("GITLAB_INSTANCE_URL", "https://gitlab.acme.internal")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _user() -> User:
    return User.new(
        email="sam@example.com",
        display_name="Sam",
        auth_provider="dev-login",
        provider_subject="sam@example.com",
    )


def _connect(
    store: JobStore,
    user: User,
    provider: RepoHostingProvider,
    scopes: list[str],
    token: str = "user-token",
) -> None:
    store.save_repo_hosting_connection(
        RepoHostingConnection.new(
            user_id=user.id,
            provider=provider,
            auth_kind=RepoHostingAuthKind.OAUTH_USER,
            account_name=f"sam-on-{provider.value.lower()}",
            account_id="1",
            account_url="https://example.com/sam",
            scopes=scopes,
            access_token_encrypted=encrypt_secret(token, provider=_CRYPTO_NAME[provider]),
            access_token_expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )


def _basic(username: str, token: str) -> str:
    return "Authorization: Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()


# --- provider resolution & header form -------------------------------------


def test_providers_are_matched_by_exact_host(providers_configured: Settings) -> None:
    settings = providers_configured
    assert provider_for_url("https://github.com/acme/repo.git", settings) == GITHUB
    assert provider_for_url("https://gitlab.acme.internal/grp/repo.git", settings) == GITLAB
    assert provider_for_url("https://bitbucket.org/ws/repo.git", settings) == BITBUCKET
    # Look-alike and unrelated hosts never get a provider (so never a token).
    assert provider_for_url("https://github.com.evil.example/acme/repo.git", settings) is None
    assert provider_for_url("https://gitlab.com/grp/repo.git", settings) is None
    assert provider_for_url("https://example.com/repo.git", settings) is None


def test_each_provider_gets_its_own_basic_username() -> None:
    # GitLab's git endpoint rejects Bearer (verified live) — Basic only.
    assert git_auth_header(GITLAB, "t") == _basic("oauth2", "t")
    assert git_auth_header(BITBUCKET, "t") == _basic("x-token-auth", "t")
    assert git_auth_header(GITHUB, "t") == _basic("x-access-token", "t")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:acme/repo.git", "https://github.com/acme/repo.git"),
        ("ssh://git@bitbucket.org/ws/repo.git", "https://bitbucket.org/ws/repo.git"),
        ("https://gitlab.acme.internal/g/r.git", "https://gitlab.acme.internal/g/r.git"),
        ("http://gitlab.acme.internal/g/r.git", "http://gitlab.acme.internal/g/r.git"),
    ],
)
def test_ssh_remotes_are_pushed_by_their_https_url(remote: str, expected: str) -> None:
    assert https_remote_url(remote) == expected


# --- scope policy ----------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "scopes", "can_read", "can_write"),
    [
        (GITHUB, ["repo", "read:user"], True, True),
        (GITHUB, ["read:user"], False, False),
        # A GitHub App token has no scopes — its access is decided by the
        # app's installation, so GitHub (not the scope list) says no.
        (GITHUB, [], True, True),
        (GITLAB, ["read_api", "read_user", "read_repository", "write_repository"], True, True),
        # A connection made before write_repository was requested: clone-only.
        (GITLAB, ["read_api", "read_user", "read_repository"], True, False),
        # ...and one from before read_repository: neither (git 401s it).
        (GITLAB, ["read_api", "read_user"], False, False),
        (GITLAB, ["api"], True, True),
        (BITBUCKET, ["account", "repository"], True, False),
        (BITBUCKET, ["account", "repository:write"], True, True),
        (BITBUCKET, ["pullrequest"], True, False),
        (BITBUCKET, ["pullrequest:write"], True, True),
        (BITBUCKET, ["account"], False, False),
    ],
)
def test_scope_policy(
    provider: RepoHostingProvider, scopes: list[str], can_read: bool, can_write: bool
) -> None:
    connection = RepoHostingConnection.new(
        user_id="u",
        provider=provider,
        auth_kind=RepoHostingAuthKind.OAUTH_USER,
        account_name="sam",
        account_id="1",
        account_url="https://example.com/sam",
        scopes=scopes,
    )
    assert has_scope_for(connection, "read") is can_read
    assert has_scope_for(connection, "write") is can_write


# --- clone (best-effort) ---------------------------------------------------


async def test_clone_header_uses_the_owners_token_for_the_matching_host(
    providers_configured: Settings,
) -> None:
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, BITBUCKET, ["account", "repository"], token="bb-token")
    _connect(store, user, GITLAB, ["read_repository"], token="gl-token")

    header = await delegated_clone_auth_header(
        "https://bitbucket.org/ws/repo.git", user.id, store, providers_configured
    )

    assert header == _basic("x-token-auth", "bb-token")


async def test_clone_header_is_none_when_scope_or_connection_is_missing(
    providers_configured: Settings,
) -> None:
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, GITLAB, ["read_api", "read_user"])  # pre-read_repository

    for url in (
        "https://gitlab.acme.internal/g/r.git",  # stale scope
        "https://github.com/acme/repo.git",  # not connected
        "https://example.com/repo.git",  # not a provider host
    ):
        assert await delegated_clone_auth_header(url, user.id, store, providers_configured) is None


async def test_clone_header_is_none_when_the_provider_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_OAUTH_ENABLED", raising=False)
    monkeypatch.setenv("GITHUB_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, GITHUB, ["repo"])

    header = await delegated_clone_auth_header(
        "https://github.com/acme/repo.git", user.id, store, get_settings()
    )

    assert header is None
    get_settings.cache_clear()


# --- push (strict) ---------------------------------------------------------


async def test_push_auth_runs_as_the_user_with_a_write_scope(
    providers_configured: Settings,
) -> None:
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, GITLAB, ["read_repository", "write_repository"], token="gl-token")

    auth = await resolve_push_auth(
        "https://gitlab.acme.internal/g/r.git", user, store, providers_configured
    )

    assert auth.header == _basic("oauth2", "gl-token")
    assert auth.provider == GITLAB
    assert auth.account_name == "sam-on-gitlab"
    assert auth.remote_url == "https://gitlab.acme.internal/g/r.git"


async def test_push_auth_converts_an_ssh_origin_to_https(providers_configured: Settings) -> None:
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, GITHUB, ["repo"])

    auth = await resolve_push_auth(
        "git@github.com:acme/repo.git", user, store, providers_configured
    )

    assert auth.remote_url == "https://github.com/acme/repo.git"


@pytest.mark.parametrize(
    ("remote", "scopes", "code", "reason"),
    [
        (
            "https://bitbucket.org/ws/repo.git",
            None,
            ErrorCode.BRANCH_PUSH_REAUTH_REQUIRED,
            "Connect your Bitbucket account",
        ),
        (
            "https://bitbucket.org/ws/repo.git",
            ["account", "repository"],
            ErrorCode.BRANCH_PUSH_REAUTH_REQUIRED,
            "Repositories: Write",
        ),
        (
            "https://gitlab.acme.internal/g/r.git",
            ["read_repository"],
            ErrorCode.BRANCH_PUSH_REAUTH_REQUIRED,
            "write_repository",
        ),
        (
            "https://example.com/repo.git",
            None,
            ErrorCode.BRANCH_PUSH_NOT_AVAILABLE,
            "isn't supported",
        ),
        (
            "http://gitlab.acme.internal/g/r.git",
            ["write_repository"],
            ErrorCode.BRANCH_PUSH_NOT_AVAILABLE,
            "https remote",
        ),
        ("", None, ErrorCode.BRANCH_PUSH_NOT_AVAILABLE, "no remote"),
    ],
)
async def test_push_auth_explains_exactly_what_to_fix(
    providers_configured: Settings,
    remote: str,
    scopes: list[str] | None,
    code: ErrorCode,
    reason: str,
) -> None:
    store, user = JobStore(":memory:"), _user()
    if scopes is not None:
        provider = provider_for_url(https_remote_url(remote), providers_configured)
        assert provider is not None
        _connect(store, user, provider, scopes)

    with pytest.raises(AppError) as excinfo:
        await resolve_push_auth(remote, user, store, providers_configured)

    assert excinfo.value.code == code
    assert reason in excinfo.value.user_message


async def test_push_auth_requires_a_signed_in_user(providers_configured: Settings) -> None:
    with pytest.raises(AppError) as excinfo:
        await resolve_push_auth(
            "https://github.com/acme/repo.git", None, JobStore(":memory:"), providers_configured
        )
    assert excinfo.value.code == ErrorCode.BRANCH_PUSH_NOT_AVAILABLE


def test_push_identity_reports_the_account_before_any_push(
    providers_configured: Settings,
) -> None:
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, BITBUCKET, ["account", "repository:write"])

    identity = describe_push_identity(
        "https://bitbucket.org/ws/repo.git", user, store, providers_configured
    )

    assert identity.ready is True
    assert identity.provider == BITBUCKET
    assert identity.account_name == "sam-on-bitbucket"
    assert identity.reason is None


def test_a_github_app_push_is_ready_with_an_installation_note(
    providers_configured: Settings,
) -> None:
    store, user = JobStore(":memory:"), _user()
    _connect(store, user, GITHUB, [])

    identity = describe_push_identity(
        "https://github.com/acme/repo.git", user, store, providers_configured
    )

    assert identity.ready is True
    assert identity.note is not None
    assert "Contents: Read and write" in identity.note
