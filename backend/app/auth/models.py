"""Authentication models for app sessions and user ownership."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


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
