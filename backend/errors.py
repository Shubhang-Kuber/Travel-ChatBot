"""Error envelope and exception handlers.

Every non-2xx response (except /health's 503 status report) is
``{"error": {"code": ..., "message": ..., "details": [...] | null}}``.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.schemas.common import ErrorBody, ErrorResponse

log = logging.getLogger(__name__)

_HTTP_STATUS_CODES = {
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
}


class AppError(Exception):
    """Raised by services/dependencies; serialized into the error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = dict(headers or {})


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    out_headers = dict(headers or {})
    if status_code == 401:
        out_headers.setdefault("WWW-Authenticate", "Bearer")
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=out_headers or None)


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_response(exc.status_code, code, message, None, exc.headers)


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Deliberately drop "input" and "ctx": inputs may contain passwords/tokens.
    details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return error_response(422, "validation_error", "Request validation failed.", details)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "An internal error occurred.")


class UnhandledErrorMiddleware:
    """Pure-ASGI catch-all that turns unhandled exceptions into the 500 envelope.

    Starlette routes an ``Exception`` handler to ``ServerErrorMiddleware``, which wraps
    OUTSIDE every user middleware, so its 500 never passes through CORSMiddleware (no
    ``Access-Control-Allow-Origin``: the browser sees a CORS failure, not the envelope).
    Register this middleware so it sits INSIDE CORSMiddleware (add it before CORS).

    If the response has already started (e.g. mid-stream), a second response cannot be
    sent: the exception is re-raised so the server aborts the connection.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_tracking(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception:
            if started:
                raise
            log.exception("Unhandled error on %s %s", scope.get("method"), scope.get("path"))
            await error_response(500, "internal_error", "An internal error occurred.")(scope, receive, send)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)
