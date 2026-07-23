"""Authentication endpoints for session bootstrap, dev login, and logout."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.auth.github_oauth import (
    complete_github_authorization,
    create_github_authorization_request,
    list_github_repositories,
)
from app.auth.jira_oauth import (
    complete_authorization,
    create_authorization_request,
    disconnect_connection,
    get_jira_auth_status,
)
from app.auth.repo_hosting import (
    disconnect_repo_hosting_connection,
    get_repo_hosting_status,
)
from app.auth.models import (
    CurrentUser,
    DevLoginRequest,
    JiraAuthStatus,
    JiraConnectCallbackResponse,
    JiraConnectStartResponse,
    LogoutResponse,
    GitHubRepositoryListResponse,
    RepoHostingConnectCallbackResponse,
    RepoHostingConnectStartResponse,
    RepoHostingProvider,
    RepoHostingStatus,
    SessionInfo,
    UserRole,
)
from app.auth.service import (
    build_session_info,
    clear_session_cookie,
    require_current_user,
    set_session_cookie,
)
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


@router.get("/jira", response_model=JiraAuthStatus)
async def jira_status(request: Request) -> JiraAuthStatus:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message="Per-user Jira connections require app authentication to be enabled.",
        )
    return get_jira_auth_status(user, _store(request), get_settings())


@router.post("/jira/connect", response_model=JiraConnectStartResponse)
async def jira_connect(request: Request) -> JiraConnectStartResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message="Per-user Jira connections require app authentication to be enabled.",
        )
    return create_authorization_request(user, _store(request), get_settings())


@router.get("/jira/callback", response_model=JiraConnectCallbackResponse)
async def jira_callback(
    request: Request,
    code: str,
    state: str,
    error: str | None = None,
) -> JiraConnectCallbackResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message="Per-user Jira connections require app authentication to be enabled.",
        )
    if error:
        raise AppError(
            ErrorCode.JIRA_OAUTH_CALLBACK_FAILED,
            internal_detail=f"jira callback error={error}",
        )
    return await complete_authorization(
        user=user,
        store=_store(request),
        settings=get_settings(),
        code=code,
        state=state,
    )


@router.delete("/jira", response_model=LogoutResponse)
async def jira_disconnect(request: Request) -> LogoutResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message="Per-user Jira connections require app authentication to be enabled.",
        )
    disconnect_connection(user, _store(request))
    return LogoutResponse()


@router.get("/repo-hosting", response_model=RepoHostingStatus)
async def repo_hosting_status(request: Request) -> RepoHostingStatus:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message=(
                "Per-user repository provider connections require app authentication "
                "to be enabled."
            ),
        )
    return get_repo_hosting_status(user, _store(request), get_settings())


@router.post(
    "/repo-hosting/github/connect", response_model=RepoHostingConnectStartResponse
)
async def github_repo_hosting_connect(request: Request) -> RepoHostingConnectStartResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message=(
                "Per-user repository provider connections require app authentication "
                "to be enabled."
            ),
        )
    return create_github_authorization_request(user, _store(request), get_settings())


@router.get(
    "/repo-hosting/github/callback", response_model=RepoHostingConnectCallbackResponse
)
async def github_repo_hosting_callback(
    request: Request,
    code: str,
    state: str,
    error: str | None = None,
) -> RepoHostingConnectCallbackResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message=(
                "Per-user repository provider connections require app authentication "
                "to be enabled."
            ),
        )
    if error:
        raise AppError(
            ErrorCode.REPO_PROVIDER_CALLBACK_FAILED,
            internal_detail=f"github callback error={error}",
        )
    return await complete_github_authorization(
        user=user,
        store=_store(request),
        settings=get_settings(),
        code=code,
        state=state,
    )


@router.get(
    "/repo-hosting/github/repos", response_model=GitHubRepositoryListResponse
)
async def github_repo_hosting_repos(request: Request) -> GitHubRepositoryListResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message=(
                "Per-user repository provider connections require app authentication "
                "to be enabled."
            ),
        )
    return await list_github_repositories(user, _store(request))


@router.delete("/repo-hosting/{provider}", response_model=LogoutResponse)
async def repo_hosting_disconnect(
    provider: RepoHostingProvider, request: Request
) -> LogoutResponse:
    user = require_current_user(request)
    if user is None:
        raise AppError(
            ErrorCode.AUTH_NOT_AVAILABLE,
            user_message=(
                "Per-user repository provider connections require app authentication "
                "to be enabled."
            ),
        )
    disconnect_repo_hosting_connection(user, provider, _store(request))
    return LogoutResponse()
