"""Password hashing with the ``bcrypt`` library (cost 12).

Used ONLY by signup (hash) and login (verify) -- never by token validation.

bcrypt is CPU-bound (~250 ms at cost 12), so async callers must use the ``*_async``
wrappers, which run it on the threadpool instead of blocking the event loop.

bcrypt only looks at the first 72 bytes of its input (bcrypt 5 raises instead of
truncating). Signup rejects passwords over 72 UTF-8 bytes rather than silently
truncating them; ``verify_password`` treats an over-long candidate as a non-match.
"""

from __future__ import annotations

import logging
import secrets

import bcrypt
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

BCRYPT_ROUNDS = 12
MIN_PASSWORD_BYTES = 8
MAX_PASSWORD_BYTES = 72  # bcrypt's hard input limit

# Hash of a random secret nobody knows, at the same cost as real hashes. Login verifies
# against it when the email is unknown, so "no such user" costs the same as "wrong
# password" (timing parity). Built at import (~250 ms once) so the cost always matches
# BCRYPT_ROUNDS and the first unknown-email login is not slower than later ones.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_hex(32).encode("ascii"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


def password_policy_violation(password: str) -> str | None:
    """Return a human-readable reason the password is unacceptable for signup, or None."""
    try:
        size = len(password.encode("utf-8"))
    except UnicodeEncodeError:  # lone surrogates survive JSON + pydantic; reject them
        return "Password must be valid Unicode text."
    if "\x00" in password:
        return "Password must not contain NUL characters."
    if size < MIN_PASSWORD_BYTES:
        return f"Password must be at least {MIN_PASSWORD_BYTES} bytes."
    if size > MAX_PASSWORD_BYTES:
        return f"Password must be at most {MAX_PASSWORD_BYTES} bytes (UTF-8)."
    return None


def hash_password(password: str) -> str:
    """bcrypt-hash a password that satisfies the policy. Returns the ASCII ``$2b$12$...`` string."""
    reason = password_policy_violation(password)
    if reason is not None:
        raise ValueError(reason)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-cost check. ``password_hash=None`` (unknown user) burns a dummy verify.

    Every path that returns False without a real comparison still runs one bcrypt
    check, so the response time does not reveal why it failed. Never raises on
    hostile input.
    """
    try:
        candidate = password.encode("utf-8")
    except UnicodeEncodeError:
        candidate = None
    usable = (
        password_hash is not None
        and candidate is not None
        and 0 < len(candidate) <= MAX_PASSWORD_BYTES
        and b"\x00" not in candidate  # signup never stores such passwords
    )
    if not usable:
        bcrypt.checkpw(b"timing-parity", _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(candidate, password_hash.encode("ascii"))  # type: ignore[union-attr]
    except (ValueError, UnicodeEncodeError):
        # Corrupt stored hash. Fail closed; never log the hash itself.
        log.error("Stored password hash is malformed; rejecting login")
        return False


async def hash_password_async(password: str) -> str:
    return await run_in_threadpool(hash_password, password)


async def verify_password_async(password: str, password_hash: str | None) -> bool:
    return await run_in_threadpool(verify_password, password, password_hash)
