"""FastAPI dependencies. Everything is read from ``request.app.state`` (no globals),
so each ``create_app()`` instance -- and each test -- is isolated.

All are ``async def`` on purpose: a plain ``def`` dependency is run via the threadpool and
would wait behind long graph calls for one of AnyIO's 40 worker tokens.
"""

from __future__ import annotations

from fastapi import Request

from backend.config import Settings
from backend.graph import TravelGraph
from backend.ratelimit import client_ip, rate_limit

__all__ = ["chat_rate_limit", "get_graph", "get_settings"]


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_graph(request: Request) -> TravelGraph:
    return request.app.state.graph


# No accounts: chat is limited per client IP.
chat_rate_limit = rate_limit("chat", client_ip)
