"""FastAPI application factory and development startup lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.constants import API_V1_PREFIX
from app.core.logging import REQUEST_ID_HEADER, configure_logging, new_correlation_id, request_scope
from app.db.init_db import initialize_database
from app.task_queue import create_run_queue

logger = logging.getLogger(__name__)

_SAFE_VALIDATION_LOCATIONS = frozenset(
    {
        "body",
        "query",
        "path",
        "header",
        "cookie",
        "offset",
        "limit",
        "name",
        "provider_type",
        "base_url",
        "remote_model_name",
        "api_key_env",
        "api_key",
        "enabled",
        "input_price_per_million",
        "output_price_per_million",
        "default_parameters",
        "model_id",
        "benchmark_id",
        "temperature",
        "top_p",
        "max_tokens",
        "seed",
        "system_prompt",
        "concurrency",
        "read_timeout_seconds",
        "input_token_reservation",
        "lifetime_request_budget",
        "lifetime_token_budget",
        "lifetime_cost_budget_usd",
        "run_status",
        "protocol_version",
        "global_concurrency_limit",
        "provider_concurrency_limit",
        "model_concurrency_limit",
        "run_concurrency_limit",
        "global_requests_per_minute",
        "provider_requests_per_minute",
        "model_requests_per_minute",
        "run_requests_per_minute",
        "global_tokens_per_minute",
        "provider_tokens_per_minute",
        "model_tokens_per_minute",
        "run_tokens_per_minute",
        "global_lifetime_request_budget",
        "global_lifetime_token_budget",
        "global_lifetime_cost_budget_usd",
        "run_lifetime_request_budget",
        "run_lifetime_token_budget",
        "run_lifetime_cost_budget_usd",
        "backlog_limit",
        "question_quantum",
        "file",
    }
)


def _safe_validation_location(location: object) -> list[str | int]:
    if not isinstance(location, (list, tuple)):
        return ["<field>"]
    return [
        item
        if isinstance(item, int) or (isinstance(item, str) and item in _SAFE_VALIDATION_LOCATIONS)
        else "<field>"
        for item in location
    ]


def create_app() -> FastAPI:
    settings = get_settings()
    run_queue = create_run_queue(settings)
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        initialize_database()
        try:
            yield
        finally:
            if run_queue is not None:
                await run_queue.close()

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
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    application.state.run_queue = run_queue

    @application.middleware("http")
    async def correlate_request(request: Request, call_next):
        # Never reuse a client-controlled value in logs or response headers. A
        # caller could deliberately duplicate a write-only API Key into
        # X-Request-ID and otherwise force us to reflect/persist it.
        request_id = new_correlation_id()
        started = monotonic()
        with request_scope(request_id):
            try:
                response = await call_next(request)
            except Exception as exc:
                route = request.scope.get("route")
                route_template = getattr(route, "path", "<unmatched>")
                logger.error(
                    "API request failed",
                    extra={
                        "event": "api_request_failed",
                        "request_id": request_id,
                        "request_method": request.method,
                        "request_path": route_template,
                        "duration_ms": round((monotonic() - started) * 1000, 3),
                        "error_code": f"api_error:{type(exc).__name__}",
                        "result": "internal_server_error",
                    },
                )
                return JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={
                        "detail": {
                            "code": "internal_server_error",
                            "message": "An internal server error occurred",
                        }
                    },
                    headers={REQUEST_ID_HEADER: request_id},
                )
            response.headers[REQUEST_ID_HEADER] = request_id
            route = request.scope.get("route")
            route_template = getattr(route, "path", "<unmatched>")
            logger.info(
                "API request completed",
                extra={
                    "event": "api_request_completed",
                    "request_id": request_id,
                    "request_method": request.method,
                    "request_path": route_template,
                    "status_code": response.status_code,
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                },
            )
            return response

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return useful field errors without reflecting potentially sensitive input values."""

        detail = []
        for error in exc.errors():
            safe_error = {key: error[key] for key in ("type", "msg") if key in error}
            if "loc" in error:
                safe_error["loc"] = _safe_validation_location(error["loc"])
            detail.append(safe_error)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": detail}
        )

    application.include_router(api_router, prefix=API_V1_PREFIX)
    return application


app = create_app()
