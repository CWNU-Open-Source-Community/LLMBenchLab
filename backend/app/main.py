"""FastAPI application factory and development startup lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.constants import API_V1_PREFIX
from app.db.init_db import initialize_database
from app.db.session import SessionLocal
from app.runners import EvaluationTaskManager

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    task_manager = EvaluationTaskManager(SessionLocal)
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        recovered_runs = initialize_database()
        if recovered_runs:
            logger.warning("Marked %d interrupted evaluation run(s) as failed", recovered_runs)
        yield
        await task_manager.shutdown()

    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Local-first, reproducible LLM benchmark API",
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
    )
    application.state.task_manager = task_manager

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return useful field errors without reflecting potentially sensitive input values."""

        detail = [
            {key: error[key] for key in ("type", "loc", "msg") if key in error}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": detail}
        )

    application.include_router(api_router, prefix=API_V1_PREFIX)
    return application


app = create_app()
