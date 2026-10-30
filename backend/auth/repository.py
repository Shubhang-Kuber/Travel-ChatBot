"""All access to the ``users`` collection (PyMongo ``AsyncMongoClient`` database).

Touched only by signup, login, refresh and logout -- never per protected request.

State inside the user document (no extra collection, per constraint 3):
- ``refresh_sessions: [{jti, expires_at}]``: live refresh tokens, at most
  ``MAX_REFRESH_SESSIONS``, oldest first.
- ``rotated_jtis: [{jti, expires_at}]``: jtis already exchanged at /auth/refresh, at most
  ``MAX_ROTATED_JTIS``, oldest first. Expired entries are pruned. Presenting one of these
  is a genuine replay (theft signal). A jti that is in neither list (evicted by the cap,
  logged out, never issued) is merely dead.

Login and rotation each use ONE single-document update with an aggregation-pipeline
``$set``. MongoDB applies that atomically, so:
- rotation's filter (presented jti is live and unexpired) is the gate. Of N concurrent
  refreshes with the same token exactly one matches. The losers then find the jti in
  ``rotated_jtis``, because the winner's single write moved it there.
- moving the old jti from ``refresh_sessions`` to ``rotated_jtis``, appending the new
  session, pruning expired entries and capping both lists happen together. There is no
  window where a crash leaves a half-rotated state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from backend.models.user import RefreshSession, UserDocument

USERS_COLLECTION = "users"
MAX_REFRESH_SESSIONS = 5
# 5 devices refreshing every 15 min rotate ~20 jtis/hour, so replay detection covers
# roughly the last hour of rotations for the heaviest user. That covers the theft
# race, where attacker and victim refresh within one access-token lifetime of each
# other. An older replay just gets a plain 401: the token is still dead.
MAX_ROTATED_JTIS = 20


class EmailTakenError(Exception):
    """Insert hit the unique index on ``email``."""


def _users(db: Any):
    return db[USERS_COLLECTION]


async def ensure_indexes(db: Any) -> None:
    """Idempotent. Startup fails loudly if existing data violates uniqueness."""
    await _users(db).create_index([("email", ASCENDING)], unique=True, name="email_unique")


def _require_str(value: object, name: str) -> str:
    # Defence in depth against operator injection ({"$ne": ...}) if a caller ever
    # passes unvalidated input; schemas already guarantee plain strings.
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    return value


async def create_user(db: Any, *, email: str, password_hash: str, created_at: datetime) -> UserDocument:
    doc: dict[str, Any] = {
        "email": _require_str(email, "email"),
        "password_hash": _require_str(password_hash, "password_hash"),
        "created_at": created_at,
        "refresh_sessions": [],
        "rotated_jtis": [],
    }
    try:
        result = await _users(db).insert_one(doc)
    except DuplicateKeyError:
        raise EmailTakenError from None
    doc["_id"] = result.inserted_id
    return doc  # type: ignore[return-value]


async def find_by_email(db: Any, email: str) -> UserDocument | None:
    """Return ``_id``, ``email``, ``password_hash`` and ``created_at`` (no session state)."""
    return await _users(db).find_one(
        {"email": _require_str(email, "email")}, projection={"refresh_sessions": 0, "rotated_jtis": 0}
    )


def _unexpired(field: str, now: datetime, exclude_jti: str | None = None) -> dict[str, Any]:
    """``$filter`` over ``field`` keeping entries with ``expires_at > now`` (and ``jti != exclude_jti``)."""
    cond: dict[str, Any] = {"$gt": ["$$s.expires_at", now]}
    if exclude_jti is not None:
        cond = {"$and": [cond, {"$ne": ["$$s.jti", {"$literal": exclude_jti}]}]}
    return {"$filter": {"input": {"$ifNull": [f"${field}", []]}, "as": "s", "cond": cond}}


def _append_capped(existing: dict[str, Any], entry: RefreshSession, cap: int) -> dict[str, Any]:
    # $literal so no value is ever interpreted as a field path / operator.
    new = {"$literal": [{"jti": entry["jti"], "expires_at": entry["expires_at"]}]}
    return {"$slice": [{"$concatArrays": [existing, new]}, -cap]}


async def add_refresh_session(db: Any, user_id: ObjectId, session: RefreshSession, now: datetime) -> bool:
    """Append a session (pruning expired ones, keeping the newest 5). False if the user is gone."""
    pipeline = [
        {"$set": {"refresh_sessions": _append_capped(_unexpired("refresh_sessions", now), session, MAX_REFRESH_SESSIONS)}}
    ]
    result = await _users(db).update_one({"_id": user_id}, pipeline)
    return result.matched_count == 1


async def rotate_refresh_session(
    db: Any, user_id: ObjectId, old: RefreshSession, new_session: RefreshSession, now: datetime
) -> bool:
    """Atomically move live ``old`` to ``rotated_jtis`` and append ``new_session``.

    Returns False if ``old`` is not a live session (already rotated, evicted, logged
    out or unknown). The caller then classifies it with ``revoke_all_if_replayed``.
    """
    old_jti = _require_str(old["jti"], "jti")
    gate = {"_id": user_id, "refresh_sessions": {"$elemMatch": {"jti": old_jti, "expires_at": {"$gt": now}}}}
    pipeline = [
        {
            "$set": {
                "refresh_sessions": _append_capped(
                    _unexpired("refresh_sessions", now, exclude_jti=old_jti), new_session, MAX_REFRESH_SESSIONS
                ),
                "rotated_jtis": _append_capped(_unexpired("rotated_jtis", now), old, MAX_ROTATED_JTIS),
            }
        }
    ]
    result = await _users(db).update_one(gate, pipeline)
    return result.matched_count == 1


async def revoke_all_if_replayed(db: Any, user_id: ObjectId, jti: str) -> bool:
    """If ``jti`` was already rotated (a genuine replay), revoke everything. Returns True if it did.

    One conditional update: the replay check and the revocation cannot interleave
    with another write. ``rotated_jtis`` is cleared too, so further replays of the
    same stolen token are plain 401s. They do not keep logging the user out after
    they sign in again (the token stays dead either way).
    """
    result = await _users(db).update_one(
        {"_id": user_id, "rotated_jtis": {"$elemMatch": {"jti": _require_str(jti, "jti")}}},
        {"$set": {"refresh_sessions": [], "rotated_jtis": []}},
    )
    return result.matched_count == 1


async def revoke_refresh_session(db: Any, user_id: ObjectId, jti: str) -> None:
    """Remove one session (logout). Idempotent. Deliberately NOT recorded in ``rotated_jtis``."""
    await _users(db).update_one({"_id": user_id}, {"$pull": {"refresh_sessions": {"jti": _require_str(jti, "jti")}}})
