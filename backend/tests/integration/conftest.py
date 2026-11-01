"""Integration fixtures: a real app (lifespan running) over ASGI with the mock graph."""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable

import pytest

from backend.tests.helpers import Api, make_settings, running_app


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with running_app(make_settings()) as a:
        yield a


@pytest.fixture
def app_with() -> Callable[..., Any]:
    """``async with app_with(limits={...}, **settings) as api:`` for custom settings."""

    def factory(**kw: Any):
        return running_app(make_settings(**kw))

    return factory
