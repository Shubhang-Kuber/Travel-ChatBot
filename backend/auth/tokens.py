"""JWT creation and LOCAL validation (python-jose, HS256).

Pure functions: no I/O, no DB, no bcrypt. ``decode_token`` is all that
``get_current_user`` runs per protected request (hard constraint 1).

Hardening:
- ``algorithms=[HS256]`` is hard-coded, never read from the token or config, and the
  unverified header's ``alg`` must also be exactly ``HS256``. That rejects ``alg: none``
  and HS/RS confusion.
- Required claims: ``sub``, ``type``, ``iat``, ``exp`` (plus ``jti`` for refresh), each
  type- and shape-checked. ``type`` must equal the expected type, so a refresh token is
  never accepted as an access token and vice versa.
- Every failure raises the same ``InvalidTokenError`` with no detail. Callers map it to
  401 ``invalid_token``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jose import jwt

from backend.config import Settings

log = logging.getLogger(__name__)

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
TOKEN_TYPES = (ACCESS_TOKEN_TYPE, REFRESH_TOKEN_TYPE)
MAX_TOKEN_LENGTH = 4096  # ours are ~250 chars; refuse to parse anything absurd

# Always used with .fullmatch(): "$" would also match before a trailing "\n".
USER_ID_RE = re.compile(r"[0-9a-f]{24}")  # ObjectId hex: never contains ':'
JTI_RE = re.compile(r"[0-9a-f]{32}")  # uuid4().hex


class InvalidTokenError(Exception):
    """The token is rejected. Deliberately carries no reason (nothing to leak)."""


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    type: str
    iat: int
    exp: int
    jti: str | None = None


@dataclass(frozen=True)
class IssuedRefreshToken:
    token: str
    jti: str
    expires_at: datetime  # tz-aware UTC, equal to the token's ``exp``


def utcnow() -> datetime:
    """Current UTC time truncated to whole seconds (JWT times are integer seconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _check_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
        raise ValueError("user_id must be a 24-char lowercase ObjectId hex string")


def _encode(claims: dict, settings: Settings) -> str:
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def create_access_token(user_id: str, settings: Settings, *, now: datetime | None = None) -> str:
    _check_user_id(user_id)
    issued = now or utcnow()
    expires = issued + timedelta(minutes=settings.access_token_ttl_minutes)
    return _encode(
        {"sub": user_id, "type": ACCESS_TOKEN_TYPE, "iat": int(issued.timestamp()), "exp": int(expires.timestamp())},
        settings,
    )


def create_refresh_token(user_id: str, settings: Settings, *, now: datetime | None = None) -> IssuedRefreshToken:
    _check_user_id(user_id)
    issued = now or utcnow()
    expires = issued + timedelta(days=settings.refresh_token_ttl_days)
    jti = uuid.uuid4().hex
    token = _encode(
        {
            "sub": user_id,
            "type": REFRESH_TOKEN_TYPE,
            "jti": jti,
            "iat": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
        },
        settings,
    )
    return IssuedRefreshToken(token=token, jti=jti, expires_at=expires)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def decode_token(
    token: str,
    expected_type: str,
    settings: Settings,
    *,
    verify_exp: bool = True,
    now: datetime | None = None,
) -> TokenClaims:
    """Validate signature, algorithm, required claims, expiry and type. Raises ``InvalidTokenError``.

    Expiry follows RFC 7519 4.1.4: the token is valid only while ``now < exp``, so
    ``exp == now`` is expired. python-jose alone accepts ``exp == now`` (it rejects only
    ``exp < now``); the check below closes that second. It matches the refresh-session
    gate in the repository (``expires_at > now``). Pass the same ``now`` to both, so a
    refresh cannot pass decode and then fail the gate on a second boundary.

    ``verify_exp=False`` is used ONLY by logout (an expired session needs no revocation,
    but the signature is still verified, so forged tokens are still rejected).
    """
    if expected_type not in TOKEN_TYPES:
        raise ValueError(f"unknown token type {expected_type!r}")
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise InvalidTokenError
    is_refresh = expected_type == REFRESH_TOKEN_TYPE
    try:
        if jwt.get_unverified_header(token).get("alg") != ALGORITHM:
            raise InvalidTokenError
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": verify_exp,
                "verify_iat": True,
                "verify_sub": True,
                # python-jose forces verify_<claim>=True for every require_<claim>, so
                # require_exp must follow verify_exp. exp presence is still enforced
                # below (_is_int(exp)) on every path.
                "require_exp": verify_exp,
                "require_iat": True,
                "require_sub": True,
                "require_jti": is_refresh,
                "leeway": 0,
            },
        )
    except InvalidTokenError:
        raise
    except Exception as exc:  # noqa: BLE001 - hostile input: any parse failure is "invalid"
        log.debug("Token rejected (%s)", type(exc).__name__)  # type only, never the token
        raise InvalidTokenError from None

    if not isinstance(payload, dict):
        raise InvalidTokenError
    sub, typ, iat, exp, jti = (payload.get(k) for k in ("sub", "type", "iat", "exp", "jti"))
    if typ != expected_type:
        raise InvalidTokenError
    if not isinstance(sub, str) or not USER_ID_RE.fullmatch(sub):
        raise InvalidTokenError
    if not _is_int(iat) or not _is_int(exp) or exp <= iat:
        raise InvalidTokenError
    if verify_exp and exp <= int((now or datetime.now(timezone.utc)).timestamp()):
        raise InvalidTokenError
    if is_refresh:
        if not isinstance(jti, str) or not JTI_RE.fullmatch(jti):
            raise InvalidTokenError
    else:
        jti = None
    return TokenClaims(sub=sub, type=typ, iat=iat, exp=exp, jti=jti)
