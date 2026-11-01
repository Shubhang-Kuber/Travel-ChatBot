"""GET /health -- liveness of the backend process."""

from __future__ import annotations

from fastapi import APIRouter

from backend.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
