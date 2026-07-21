"""Authentication helpers for server-side sessions and ownership checks."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import cast

from fastapi import Request, Response

from app.auth.models import CurrentUser, SessionInfo, User, UserRole
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job
from app.jobs.store import JobStore


@dataclass(frozen=True)
class Identity:
    email: str
    display_name: str
    auth_provider: str
    provider_subject: str


def _store(request: Request) -> JobStore:
    return cast(JobStore, request.app.state.job_store)


def _role_for_email(email: str, settings: Settings) -> UserRole:
    admin_emails = {value.lower() for value in settings.auth_admin_emails}
    return UserRole.ADMIN if email.lower() in admin_emails else UserRole.USER


def _cookie_name(settings: Settings) -> str:
    return settings.auth_session_cookie_name


def _trusted_proxy_allows_request(request: Request, settings: Settings) -> bool:
    if not settings.auth_trusted_proxy_enabled:
        return False

    client = request.client
    host = client.host.strip() if client and client.host else ""
    if not host:
        return False

    for source in settings.auth_trusted_proxy_sources:
        candidate = source.strip()
        if not candidate:
            continue
        if candidate.lower() == host.lower():
            return True
        try:
            if ip_address(host) in ip_network(candidate, strict=False):
                return True
        except ValueError:
            continue
    return False


def _trusted_identity(request: Request, settings: Settings) -> Identity | None:
    if not _trusted_proxy_allows_request(request, settings):
        return None

    email = request.headers.get(settings.auth_trusted_email_header, "").strip()
    if not email:
        return None
    display_name = request.headers.get(settings.auth_trusted_name_header, "").strip() or email
    provider_subject = (
        request.headers.get(settings.auth_trusted_subject_header, "").strip() or email.lower()
    )
    return Identity(
        email=email.lower(),
        display_name=display_name,
        auth_provider=settings.auth_trusted_provider_name,
        provider_subject=provider_subject,
    )


def _upsert_identity_user(request: Request, identity: Identity, settings: Settings) -> User:
    return _store(request).upsert_user(
        email=identity.email,
        display_name=identity.display_name,
        auth_provider=identity.auth_provider,
        provider_subject=identity.provider_subject,
        role=_role_for_email(identity.email, settings),
    )


def _resolve_session_user(request: Request, settings: Settings) -> User | None:
    session_id = request.cookies.get(_cookie_name(settings))
    if not session_id:
        return None
    session = _store(request).get_session(session_id)
    if session is None:
        return None
    return _store(request).get_user(session.user_id)


def resolve_current_user(request: Request) -> User | None:
    settings = get_settings()
    if not settings.auth_enabled:
        return None

    user = _resolve_session_user(request, settings)
    if user is not None:
        return user

    identity = _trusted_identity(request, settings)
    if identity is None:
        return None
    return _upsert_identity_user(request, identity, settings)


def require_current_user(request: Request) -> User | None:
    settings = get_settings()
    if not settings.auth_enabled:
        return None
    user = resolve_current_user(request)
    if user is None:
        raise AppError(ErrorCode.UNAUTHENTICATED)
    return user


def ensure_job_access(job: Job, user: User | None) -> None:
    settings = get_settings()
    if not settings.auth_enabled:
        return
    if user is None:
        raise AppError(ErrorCode.UNAUTHENTICATED)
    if user.role == UserRole.ADMIN:
        return
    if job.owner_user_id != user.id:
        raise AppError(ErrorCode.FORBIDDEN)


def set_session_cookie(response: Response, session_id: str, settings: Settings) -> None:
    response.set_cookie(
        key=_cookie_name(settings),
        value=session_id,
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="lax",
        max_age=settings.auth_session_ttl_hours * 3600,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=_cookie_name(settings),
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="lax",
        path="/",
    )


def build_session_info(
    request: Request,
    response: Response | None = None,
    *,
    allow_bootstrap: bool,
) -> SessionInfo:
    settings = get_settings()
    if not settings.auth_enabled:
        return SessionInfo(auth_enabled=False, can_dev_login=False)

    user = _resolve_session_user(request, settings)
    if user is None and allow_bootstrap:
        identity = _trusted_identity(request, settings)
        if identity is not None:
            user = _upsert_identity_user(request, identity, settings)
            if response is not None:
                session = _store(request).create_session(user.id, settings.auth_session_ttl_hours)
                set_session_cookie(response, session.id, settings)

    return SessionInfo(
        auth_enabled=True,
        can_dev_login=settings.auth_allow_dev_login,
        user=CurrentUser.from_user(user) if user is not None else None,
    )
