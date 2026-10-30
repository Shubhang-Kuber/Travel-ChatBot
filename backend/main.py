"""FastAPI application factory. Run with ``uvicorn backend.main:app`` (1 worker)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import backend.auth
from backend.config import Settings, get_settings
from backend.db import create_client, wait_for_mongo
from backend.errors import UnhandledErrorMiddleware, install_exception_handlers
from backend.graph import load_graph
from backend.ratelimit import RateLimiter
from backend.routes import auth, chat, health

log = logging.getLogger(__name__)

ROUTERS = (health.router, auth.router, chat.router)

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


def create_app(settings: Settings | None = None, mongo_client: Any | None = None) -> FastAPI:
    """Build an isolated app. Inject ``mongo_client`` in tests; it will not be closed."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_client = mongo_client is None
        client = create_client(settings) if owns_client else mongo_client
        try:
            await wait_for_mongo(client, settings.mongo_startup_timeout_seconds)
            db = client[settings.mongo_db]
            await backend.auth.ensure_indexes(db)
            app.state.mongo_client = client
            app.state.db = db
            app.state.graph = load_graph(settings)
            yield
        finally:
            if owns_client:
                await client.close()

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
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Retry-After"],
        allow_credentials=False,  # bearer headers, not cookies
    )
    install_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy module-level ``app`` so importing backend.main (e.g. in tests that call
    # create_app with their own Settings) does not require JWT_SECRET in the env.
    if name == "app":
        app = create_app()
        globals()["app"] = app
        return app
    raise AttributeError(name)
