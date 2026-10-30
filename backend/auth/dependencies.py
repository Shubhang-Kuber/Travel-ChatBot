"""``get_current_user``: LOCAL JWT validation only -- zero DB access, zero bcrypt.

Reads settings from ``request.app.state.settings``. Must not import
``backend.dependencies`` (circular import).
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.auth.tokens import ACCESS_TOKEN_TYPE, InvalidTokenError, decode_token
from backend.errors import AppError
from backend.models.user import CurrentUser

# auto_error=False: we raise our own envelope (401 not_authenticated), not FastAPI's
# default error. Using HTTPBearer also documents the scheme in OpenAPI ("Authorize").
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login or /auth/refresh.")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if credentials is None or not credentials.credentials.strip():
        raise AppError(401, "not_authenticated", "Missing bearer token.")
    try:
        claims = decode_token(credentials.credentials.strip(), ACCESS_TOKEN_TYPE, request.app.state.settings)
    except InvalidTokenError:
        raise AppError(401, "invalid_token", "The access token is invalid or has expired.") from None
    return CurrentUser(user_id=claims.sub)
