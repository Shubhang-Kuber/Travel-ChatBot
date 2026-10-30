"""/auth/* -- thin handlers: validate (schemas), rate-limit per client IP, delegate to the service."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from backend.auth import service
from backend.config import Settings
from backend.dependencies import get_db, get_settings
from backend.ratelimit import client_ip, rate_limit
from backend.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)
from backend.schemas.common import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {"model": ErrorResponse} for code in codes}


# Logout shares the "refresh" bucket (same IP counter): both endpoints take a refresh
# token, and config defines only login/signup/refresh/chat buckets.
_signup_limit = rate_limit("signup", client_ip)
_login_limit = rate_limit("login", client_ip)
_refresh_limit = rate_limit("refresh", client_ip)


@router.post(
    "/signup",
    status_code=201,
    response_model=UserResponse,
    dependencies=[Depends(_signup_limit)],
    responses=_errors(409, 422, 429),
)
async def signup(body: SignupRequest, db: Any = Depends(get_db)) -> UserResponse:
    return await service.signup(db, body.email, body.password)


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(_login_limit)],
    responses=_errors(401, 422, 429),
)
async def login(
    body: LoginRequest, db: Any = Depends(get_db), settings: Settings = Depends(get_settings)
) -> TokenResponse:
    return await service.login(db, settings, body.email, body.password)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(_refresh_limit)],
    responses=_errors(401, 422, 429),
)
async def refresh(
    body: RefreshRequest, db: Any = Depends(get_db), settings: Settings = Depends(get_settings)
) -> TokenResponse:
    return await service.refresh(db, settings, body.refresh_token)


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(_refresh_limit)],
    responses=_errors(401, 422, 429),
)
async def logout(
    body: LogoutRequest, db: Any = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Response:
    await service.logout(db, settings, body.refresh_token)
    return Response(status_code=204)
