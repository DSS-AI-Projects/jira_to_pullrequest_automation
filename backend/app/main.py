"""FastAPI app factory.

Error handling contract (security invariant 4): every response the client can
ever see is either a route's typed payload or {"error": {code, message}} built
from the user-safe error catalog. Raw exception text and stack traces never
leave the server; they are logged through the redactor instead.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import configure_logging, get_logger
from app.jobs.runner import JobSteps
from app.jobs.store import JobStore
from app.steps import default_steps

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
    app.include_router(api_router)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
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
