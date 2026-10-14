"""FastAPI application initialization with middleware and exception handling."""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.api.routers import ingest
from app.core.config import get_settings
from app.core.errors import AppError, PayloadTooLargeError
from app.core.logging import request_id_ctx_var, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle hook."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    yield


app = FastAPI(
    title="Aurik Industrial Equipment Monitoring API",
    version="1.0.0",
    description="Industrial equipment telemetry normalization and operational attention service.",
    lifespan=lifespan,
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Inject a unique X-Request-ID header and populate contextvars for structured logging."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = request_id_ctx_var.set(req_id)
        try:
            # Check maximum payload size limit via Content-Length header
            content_length = request.headers.get("Content-Length")
            settings = get_settings()
            if content_length and int(content_length) > settings.MAX_PAYLOAD_BYTES:
                raise PayloadTooLargeError(
                    f"Payload size {content_length} bytes exceeds maximum allowed limit of {settings.MAX_PAYLOAD_BYTES} bytes."
                )

            response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            return response
        finally:
            request_id_ctx_var.reset(token)


app.add_middleware(RequestContextMiddleware)


# ---------------------------------------------------------------------------
# Global Exception Handlers (Standard Error Envelope)
# ---------------------------------------------------------------------------
@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """Format known application and domain exceptions into canonical error envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Format FastAPI / Pydantic validation errors into canonical error envelope."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request payload failed schema validation.",
                "details": exc.errors(),
            }
        },
    )


@app.exception_handler(Exception)
async def handle_generic_exception(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for unhandled exceptions."""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected server error occurred.",
                "details": str(exc),
            }
        },
    )


# ---------------------------------------------------------------------------
# Router Inclusions
# ---------------------------------------------------------------------------
app.include_router(ingest.router)
