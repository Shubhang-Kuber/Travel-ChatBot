"""GET /health -- liveness of the backend and its MongoDB connection.

Deliberately exempt from the error envelope: a 503 here is a status report for probes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.db import ping
from backend.dependencies import get_mongo_client
from backend.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
async def health(client=Depends(get_mongo_client)) -> JSONResponse:
    if await ping(client):
        return JSONResponse(status_code=200, content=HealthResponse(status="ok", mongo="up").model_dump())
    return JSONResponse(status_code=503, content=HealthResponse(status="degraded", mongo="down").model_dump())
