"""Authentication endpoints for session bootstrap, dev login, and logout."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.auth.models import (
    CurrentUser,
    DevLoginRequest,
    LogoutResponse,
    SessionInfo,
    UserRole,
)
from app.auth.service import build_session_info, clear_session_cookie, set_session_cookie
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.jobs.store import JobStore

router = APIRouter(prefix="/api/auth")


def _store(request: Request) -> JobStore:
    return request.app.state.job_store


@router.get("/session", response_model=SessionInfo)
async def session_info(request: Request, response: Response) -> SessionInfo:
    return build_session_info(request, response, allow_bootstrap=True)


@router.post("/dev-login", response_model=SessionInfo)
async def dev_login(
    payload: DevLoginRequest, request: Request, response: Response
) -> SessionInfo:
    settings = get_settings()
    if not settings.auth_enabled or not settings.auth_allow_dev_login:
        raise AppError(ErrorCode.AUTH_NOT_AVAILABLE)

    email = payload.email.strip().lower()
    display_name = payload.display_name.strip() or email
    store = _store(request)
    admin_emails = {value.lower() for value in settings.auth_admin_emails}
    role = UserRole.ADMIN if email in admin_emails else UserRole.USER
    user = store.upsert_user(
        email=email,
        display_name=display_name,
        auth_provider="dev-login",
        provider_subject=email,
        role=role,
    )
    session = store.create_session(user.id, settings.auth_session_ttl_hours)
    set_session_cookie(response, session.id, settings)
    return SessionInfo(
        auth_enabled=True,
        can_dev_login=settings.auth_allow_dev_login,
        user=CurrentUser.from_user(user),
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request, response: Response) -> LogoutResponse:
    settings = get_settings()
    session_id = request.cookies.get(settings.auth_session_cookie_name)
    if session_id:
        _store(request).delete_session(session_id)
    clear_session_cookie(response, settings)
    return LogoutResponse()
