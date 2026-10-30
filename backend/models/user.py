"""User domain types: the authenticated principal and the stored ``users`` document."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from bson import ObjectId


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated principal, derived from the access token alone (no DB lookup).

    ``user_id`` is the user's ObjectId as 24 lowercase hex chars. It never contains ':'
    (the graph thread id is ``f"{user_id}:{thread_id}"``).
    """

    user_id: str


class RefreshSession(TypedDict):
    """One live refresh token, identified by its ``jti``. Capped at 5 per user."""

    jti: str
    expires_at: datetime  # tz-aware UTC; equals the refresh token's ``exp``


# Functional syntax because ``_id`` is not a valid class-body identifier for TypedDict.
UserDocument = TypedDict(
    "UserDocument",
    {
        "_id": ObjectId,
        "email": str,  # normalized: stripped + lowercased; unique index
        "password_hash": str,  # bcrypt "$2b$12$..."; never logged or returned
        "created_at": datetime,  # tz-aware UTC, millisecond precision (BSON date)
        "refresh_sessions": list[RefreshSession],  # live refresh tokens, newest last, cap 5
        # jtis already exchanged at /auth/refresh (same {jti, expires_at} shape), newest
        # last, cap 20, expired entries pruned. Presenting one = replay -> revoke all.
        "rotated_jtis": list[RefreshSession],
    },
)
