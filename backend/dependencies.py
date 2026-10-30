"""FastAPI dependencies. Everything is read from ``request.app.state`` (no globals),
so each ``create_app()`` instance -- and each test -- is isolated.

All are ``async def`` on purpose: a plain ``def`` dependency is run via the threadpool and
would wait for one of AnyIO's 40 worker tokens, which graph calls and bcrypt can hold for
seconds (/health was observed to take ~1.8 s with 40 chats in flight).
"""

from __future__ import annotations

from fastapi import Depends, Request

from backend.auth.dependencies import get_current_user
from backend.config import Settings
from backend.graph import TravelGraph
from backend.models.user import CurrentUser
from backend.ratelimit import rate_limit

__all__ = [
    "CurrentUser",
    "chat_rate_limit",
    "current_user_key",
    "get_current_user",
    "get_db",
    "get_graph",
    "get_mongo_client",
    "get_settings",
]


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_mongo_client(request: Request):  # -> pymongo.AsyncMongoClient
    return request.app.state.mongo_client


async def get_db(request: Request):  # -> pymongo.asynchronous.database.AsyncDatabase
    return request.app.state.db


async def get_graph(request: Request) -> TravelGraph:
    return request.app.state.graph


async def current_user_key(user: CurrentUser = Depends(get_current_user)) -> str:
    """Rate-limit key for authenticated routes: the user id (cached per request by FastAPI)."""
    return user.user_id


chat_rate_limit = rate_limit("chat", current_user_key)
