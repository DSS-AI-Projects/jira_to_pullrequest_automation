"""Authentication models for app sessions and user ownership."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from secrets import token_urlsafe

from pydantic import BaseModel, ConfigDict


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class JiraAuthMode(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    SHARED = "SHARED"
    DELEGATED = "DELEGATED"


class RepoHostingProvider(StrEnum):
    GITHUB = "GITHUB"
    GITLAB = "GITLAB"


class RepoHostingAuthKind(StrEnum):
    OAUTH_USER = "OAUTH_USER"
    APP_INSTALLATION = "APP_INSTALLATION"


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    display_name: str
    auth_provider: str
    provider_subject: str
    role: UserRole = UserRole.USER
    created_at: datetime
    last_login_at: datetime

    @classmethod
    def new(
        cls,
        *,
        email: str,
        display_name: str,
        auth_provider: str,
        provider_subject: str,
        role: UserRole = UserRole.USER,
    ) -> User:
        now = datetime.now(UTC)
        return cls(
            id=uuid.uuid4().hex,
            email=email,
            display_name=display_name,
            auth_provider=auth_provider,
            provider_subject=provider_subject,
            role=role,
            created_at=now,
            last_login_at=now,
        )


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    expires_at: datetime
    created_at: datetime

    @classmethod
    def new(cls, *, user_id: str, ttl_hours: int) -> Session:
        now = datetime.now(UTC)
        return cls(
            id=uuid.uuid4().hex,
            user_id=user_id,
            expires_at=now + timedelta(hours=ttl_hours),
            created_at=now,
        )

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)


class CurrentUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    display_name: str
    role: UserRole

    @classmethod
    def from_user(cls, user: User) -> CurrentUser:
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
        )


class SessionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_enabled: bool
    can_dev_login: bool
    user: CurrentUser | None = None


class DevLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    display_name: str


class LogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True


class JiraCloudSite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    url: str


class JiraConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    site: JiraCloudSite
    scopes: list[str]
    access_token_encrypted: str
    refresh_token_encrypted: str | None = None
    access_token_expires_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(
        cls,
        *,
        user_id: str,
        site: JiraCloudSite,
        scopes: list[str],
        access_token_encrypted: str,
        refresh_token_encrypted: str | None,
        access_token_expires_at: datetime,
    ) -> JiraConnection:
        now = datetime.now(UTC)
        return cls(
            user_id=user_id,
            site=site,
            scopes=scopes,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            access_token_expires_at=access_token_expires_at,
            created_at=now,
            updated_at=now,
        )


class JiraConnectionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: JiraCloudSite
    scopes: list[str]
    connected_at: datetime
    updated_at: datetime
    access_token_expires_at: datetime
    has_refresh_token: bool

    @classmethod
    def from_connection(cls, connection: JiraConnection) -> JiraConnectionInfo:
        return cls(
            site=connection.site,
            scopes=connection.scopes,
            connected_at=connection.created_at,
            updated_at=connection.updated_at,
            access_token_expires_at=connection.access_token_expires_at,
            has_refresh_token=connection.refresh_token_encrypted is not None,
        )


class JiraAuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oauth_enabled: bool
    oauth_configured: bool
    shared_configured: bool
    effective_mode: JiraAuthMode
    connected: bool
    connection: JiraConnectionInfo | None = None


class JiraConnectStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_url: str


class JiraConnectCallbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    connection: JiraConnectionInfo


class JiraOAuthState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    user_id: str
    expires_at: datetime
    created_at: datetime

    @classmethod
    def new(cls, *, user_id: str, ttl_minutes: int) -> JiraOAuthState:
        now = datetime.now(UTC)
        return cls(
            state=token_urlsafe(32),
            user_id=user_id,
            expires_at=now + timedelta(minutes=ttl_minutes),
            created_at=now,
        )

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)


class RepoHostingConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    provider: RepoHostingProvider
    auth_kind: RepoHostingAuthKind
    account_name: str
    account_id: str
    account_url: str
    scopes: list[str]
    access_token_encrypted: str | None = None
    refresh_token_encrypted: str | None = None
    access_token_expires_at: datetime | None = None
    installation_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(
        cls,
        *,
        user_id: str,
        provider: RepoHostingProvider,
        auth_kind: RepoHostingAuthKind,
        account_name: str,
        account_id: str,
        account_url: str,
        scopes: list[str],
        access_token_encrypted: str | None = None,
        refresh_token_encrypted: str | None = None,
        access_token_expires_at: datetime | None = None,
        installation_id: str | None = None,
    ) -> RepoHostingConnection:
        now = datetime.now(UTC)
        return cls(
            user_id=user_id,
            provider=provider,
            auth_kind=auth_kind,
            account_name=account_name,
            account_id=account_id,
            account_url=account_url,
            scopes=scopes,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            access_token_expires_at=access_token_expires_at,
            installation_id=installation_id,
            created_at=now,
            updated_at=now,
        )


class RepoHostingConnectionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: RepoHostingProvider
    auth_kind: RepoHostingAuthKind
    account_name: str
    account_id: str
    account_url: str
    scopes: list[str]
    installation_id: str | None = None
    connected_at: datetime
    updated_at: datetime
    access_token_expires_at: datetime | None = None
    has_refresh_token: bool = False

    @classmethod
    def from_connection(
        cls, connection: RepoHostingConnection
    ) -> RepoHostingConnectionInfo:
        return cls(
            provider=connection.provider,
            auth_kind=connection.auth_kind,
            account_name=connection.account_name,
            account_id=connection.account_id,
            account_url=connection.account_url,
            scopes=connection.scopes,
            installation_id=connection.installation_id,
            connected_at=connection.created_at,
            updated_at=connection.updated_at,
            access_token_expires_at=connection.access_token_expires_at,
            has_refresh_token=connection.refresh_token_encrypted is not None,
        )


class RepoHostingProviderStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: RepoHostingProvider
    display_name: str
    enabled: bool
    configured: bool
    connected: bool
    connection: RepoHostingConnectionInfo | None = None


class RepoHostingStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[RepoHostingProviderStatus]


class RepoHostingConnectStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_url: str


class RepoHostingConnectCallbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    connection: RepoHostingConnectionInfo


class GitHubRepositorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    full_name: str
    html_url: str
    clone_url: str
    default_branch: str | None = None
    owner_login: str
    private: bool


class GitHubRepositoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[GitHubRepositorySummary]


class ProviderOAuthState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    user_id: str
    provider: RepoHostingProvider
    expires_at: datetime
    created_at: datetime

    @classmethod
    def new(
        cls, *, user_id: str, provider: RepoHostingProvider, ttl_minutes: int
    ) -> ProviderOAuthState:
        now = datetime.now(UTC)
        return cls(
            state=token_urlsafe(32),
            user_id=user_id,
            provider=provider,
            expires_at=now + timedelta(minutes=ttl_minutes),
            created_at=now,
        )

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)
