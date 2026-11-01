"""FastAPI application factory. Run with ``uvicorn backend.main:app`` (1 worker)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import Settings, get_settings
from backend.errors import UnhandledErrorMiddleware, install_exception_handlers
from backend.graph import load_graph
from backend.ratelimit import RateLimiter
from backend.routes import chat, health

log = logging.getLogger(__name__)

ROUTERS = (health.router, chat.router)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_HANDLER_MARK = "_wanderchat_handler"


def configure_logging(level: str) -> None:
    """Give the ``backend`` logger a stderr handler (once) and set its level.

    Idempotent: repeated ``create_app()`` calls (tests) never stack handlers. The root and
    ``uvicorn.*`` loggers are left alone. ``propagate`` stays True, so pytest's caplog
    (a root handler) still captures backend records. Our own handler is not on root, so
    a default uvicorn run (which configures no root handler) prints each line once.
    """
    logger = logging.getLogger("backend")
    logger.setLevel(level)
    if not any(getattr(h, _HANDLER_MARK, False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        setattr(handler, _HANDLER_MARK, True)
        logger.addHandler(handler)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an isolated app (tests pass their own ``Settings``)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.graph = load_graph(settings)
        yield

    app = FastAPI(title="WanderChat API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter()

    # add_middleware prepends, so the error middleware added FIRST ends up INSIDE CORS:
    # ServerErrorMiddleware -> CORSMiddleware -> UnhandledErrorMiddleware -> ExceptionMiddleware -> router.
    # Its 500 envelope therefore gets CORS headers (BUG-2).
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        expose_headers=["Retry-After"],
        allow_credentials=False,
    )
    install_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy module-level ``app`` so importing backend.main (e.g. in tests that call
    # create_app with their own Settings) does not read the env at import time.
    if name == "app":
        app = create_app()
        globals()["app"] = app
        return app
    raise AttributeError(name)
