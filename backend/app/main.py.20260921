"""FastAPI app factory.

Error handling contract (security invariant 4): every response the client can
ever see is either a route's typed payload or {"error": {code, message}} built
from the user-safe error catalog. Raw exception text and stack traces never
leave the server; they are logged through the redactor instead.
"""

from __future__ import annotations

import truststore

# Must run before any ssl.SSLContext is created (httpx builds one on first use
# inside jira_oauth.py/github_oauth.py/gitlab_oauth.py) — swaps Python's default
# certifi-only trust store for the OS's own (Windows Certificate Store /
# macOS Keychain / OpenSSL default on Linux), so an outbound HTTPS call to an
# internal/self-hosted provider instance (e.g. a corporate GitLab behind an
# internal CA already trusted by the OS, the way a browser trusts it) verifies
# correctly instead of failing with CERTIFICATE_VERIFY_FAILED.
truststore.inject_into_ssl()

from collections.abc import AsyncGenerator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.api.auth import router as auth_router  # noqa: E402
from app.api.routes import router as api_router  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.errors import AppError, ErrorCode  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.jobs.runner import JobSteps  # noqa: E402
from app.jobs.store import JobStore  # noqa: E402
from app.steps import default_steps  # noqa: E402

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # (Re)attach the redaction filter after uvicorn installs its own handlers.
    configure_logging()
    yield


def create_app(store: JobStore | None = None, steps: JobSteps | None = None) -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(title="jira2pullreq", lifespan=_lifespan)
    app.state.job_store = store if store is not None else JobStore(settings.db_path)
    app.state.job_steps = steps if steps is not None else default_steps()
    app.include_router(auth_router)
    app.include_router(api_router)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.internal_detail:
            logger.warning("AppError %s: %s", exc.code, exc.internal_detail)
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Report field locations only — never echo submitted values back.
        fields = sorted(
            {".".join(str(part) for part in err.get("loc", ())) for err in exc.errors()}
        )
        error = AppError(
            ErrorCode.INPUT_INVALID,
            user_message="Invalid input in: " + (", ".join(fields) or "request body"),
        )
        return JSONResponse(status_code=error.http_status, content=error.to_payload())

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        error = AppError(ErrorCode.INTERNAL)
        return JSONResponse(status_code=error.http_status, content=error.to_payload())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
