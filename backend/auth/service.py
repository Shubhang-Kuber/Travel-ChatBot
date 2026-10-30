"""Auth use cases: signup, login, refresh (rotation + reuse detection), logout.

Logging: user ids and event names only. Never emails of failed logins, passwords,
hashes or tokens.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from backend.auth import repository as repo
from backend.auth.passwords import hash_password_async, verify_password_async
from backend.auth.tokens import (
    REFRESH_TOKEN_TYPE,
    InvalidTokenError,
    TokenClaims,
    create_access_token,
    create_refresh_token,
    decode_token,
    utcnow,
)
from backend.config import Settings
from backend.errors import AppError
from backend.schemas.auth import TokenResponse, UserResponse

log = logging.getLogger(__name__)

_INVALID_CREDENTIALS_MESSAGE = "Invalid email or password."
_INVALID_REFRESH_MESSAGE = "The refresh token is invalid, expired or revoked."


def _invalid_credentials() -> AppError:
    # One body for "no such user" and "wrong password" (no user enumeration via login).
    return AppError(401, "invalid_credentials", _INVALID_CREDENTIALS_MESSAGE)


def _invalid_refresh() -> AppError:
    return AppError(401, "invalid_token", _INVALID_REFRESH_MESSAGE)


def _decode_refresh(
    token: str, settings: Settings, *, verify_exp: bool = True, now: datetime | None = None
) -> TokenClaims:
    try:
        return decode_token(token, REFRESH_TOKEN_TYPE, settings, verify_exp=verify_exp, now=now)
    except InvalidTokenError:
        raise _invalid_refresh() from None


def _now_ms() -> datetime:
    # BSON dates have millisecond precision; truncate so what we return equals what is stored.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


async def _issue_tokens(db: Any, settings: Settings, user_id: ObjectId) -> TokenResponse:
    now = utcnow()
    sub = str(user_id)
    access = create_access_token(sub, settings, now=now)
    refresh = create_refresh_token(sub, settings, now=now)
    stored = await repo.add_refresh_session(db, user_id, {"jti": refresh.jti, "expires_at": refresh.expires_at}, now)
    if not stored:  # user deleted between lookup and session write: fail closed
        raise _invalid_credentials()
    return _token_response(access, refresh.token, settings)


def _token_response(access: str, refresh: str, settings: Settings) -> TokenResponse:
    return TokenResponse(
        access_token=access, refresh_token=refresh, expires_in=settings.access_token_ttl_minutes * 60
    )


async def signup(db: Any, email: str, password: str) -> UserResponse:
    # Hash BEFORE touching the DB, so taken and free emails cost the same time. The
    # 409 still reveals that the email is registered (accepted tradeoff; rate-limited).
    password_hash = await hash_password_async(password)
    try:
        user = await repo.create_user(db, email=email, password_hash=password_hash, created_at=_now_ms())
    except repo.EmailTakenError:
        raise AppError(409, "email_taken", "An account with this email already exists.") from None
    log.info("User signed up user_id=%s", user["_id"])
    return UserResponse(id=str(user["_id"]), email=user["email"], created_at=user["created_at"])


async def login(db: Any, settings: Settings, email: str, password: str) -> TokenResponse:
    user = await repo.find_by_email(db, email)
    # Always run exactly one bcrypt verify; unknown users verify against a dummy hash.
    ok = await verify_password_async(password, user["password_hash"] if user is not None else None)
    if user is None or not ok:
        log.info("Login failed")
        raise _invalid_credentials()
    tokens = await _issue_tokens(db, settings, user["_id"])
    log.info("Login succeeded user_id=%s", user["_id"])
    return tokens


async def refresh(db: Any, settings: Settings, refresh_token: str) -> TokenResponse:
    # One `now` for both decode (exp > now) and the DB gate (expires_at > now), so the
    # two can never disagree on the expiry second.
    now = utcnow()
    claims = _decode_refresh(refresh_token, settings, now=now)
    user_id = ObjectId(claims.sub)
    access = create_access_token(claims.sub, settings, now=now)
    new_refresh = create_refresh_token(claims.sub, settings, now=now)
    old_session = {"jti": claims.jti, "expires_at": datetime.fromtimestamp(claims.exp, timezone.utc)}
    rotated = await repo.rotate_refresh_session(
        db, user_id, old_session, {"jti": new_refresh.jti, "expires_at": new_refresh.expires_at}, now  # type: ignore[arg-type]
    )
    if rotated:
        return _token_response(access, new_refresh.token, settings)
    # Validly signed and unexpired, but not a live session.
    if await repo.revoke_all_if_replayed(db, user_id, claims.jti):  # type: ignore[arg-type]
        # It was already exchanged once: a replay (or a lost race between two clients
        # holding the same token). Assume theft, so every session is now revoked and
        # whoever holds the newer tokens must log in again.
        log.warning("Refresh token replay detected; all sessions revoked user_id=%s", claims.sub)
    else:
        # Evicted by the 5-session cap, logged out, or never stored: just dead.
        log.info("Refresh rejected: session not live user_id=%s", claims.sub)
    raise _invalid_refresh()


async def logout(db: Any, settings: Settings, refresh_token: str) -> None:
    """Revoke one refresh session. Idempotent.

    - Forged, malformed or wrong-type token: 401 invalid_token (we never act on
      unauthenticated input).
    - Validly signed but expired: 204. The session is already dead, and a user
      logging out after a week away should not see an error.
    - Validly signed but already revoked or rotated: 204, and no revoke-all (the
      lead's reuse rule applies to /auth/refresh only).
    """
    claims = _decode_refresh(refresh_token, settings, verify_exp=False)
    await repo.revoke_refresh_session(db, ObjectId(claims.sub), claims.jti)  # type: ignore[arg-type]
    log.info("Logout user_id=%s", claims.sub)
