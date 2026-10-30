"""Shared response schemas: the error envelope and the health report."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[Any] | None = None


class ErrorResponse(BaseModel):
    """Every non-2xx response (except /health's 503) has this shape."""

    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    mongo: Literal["up", "down"]
