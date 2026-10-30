"""Token contract: HS256 only, signature + expiry + type checked locally, claims required.

Hostile tokens are crafted with python-jose / raw base64 directly, not through the
module under test, so the tests do not depend on how tokens.py builds them.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from backend.auth.tokens import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    utcnow,
)
from backend.tests.helpers import OTHER_SECRET, TEST_SECRET, USER_ID, make_settings

pytestmark = pytest.mark.unit

SETTINGS = make_settings()


def b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()


def claims(typ: str = "access", **over) -> dict:
    now = int(time.time())
    c = {"sub": USER_ID, "type": typ, "iat": now, "exp": now + 600}
    if typ == "refresh":
        c["jti"] = "0123456789abcdef0123456789abcdef"
    c.update(over)
    return {k: v for k, v in c.items() if v is not ...}


def hs256(c: dict, secret: str = TEST_SECRET) -> str:
    return jwt.encode(c, secret, algorithm="HS256")


def rejected(token: str, typ: str = "access", **kw) -> bool:
    try:
        decode_token(token, typ, SETTINGS, **kw)
    except InvalidTokenError:
        return True
    return False


# ------------------------------------------------------------------ happy paths


def test_access_round_trip() -> None:
    tok = create_access_token(USER_ID, SETTINGS)
    c = decode_token(tok, "access", SETTINGS)
    assert c.sub == USER_ID and c.type == "access"
    assert c.exp - c.iat == 15 * 60


def test_access_header_and_claims_on_the_wire() -> None:
    tok = create_access_token(USER_ID, SETTINGS)
    assert jwt.get_unverified_header(tok)["alg"] == "HS256"
    raw = jwt.get_unverified_claims(tok)
    assert set(raw) == {"sub", "type", "iat", "exp"}


def test_refresh_round_trip() -> None:
    issued = create_refresh_token(USER_ID, SETTINGS)
    c = decode_token(issued.token, "refresh", SETTINGS)
    assert c.sub == USER_ID and c.type == "refresh" and c.jti == issued.jti
    assert len(issued.jti) == 32 and int(issued.jti, 16) >= 0
    assert c.exp - c.iat == 7 * 24 * 3600
    assert int(issued.expires_at.timestamp()) == c.exp
    raw = jwt.get_unverified_claims(issued.token)
    assert set(raw) == {"sub", "type", "jti", "iat", "exp"}


def test_refresh_jti_unique() -> None:
    assert create_refresh_token(USER_ID, SETTINGS).jti != create_refresh_token(USER_ID, SETTINGS).jti


def test_ttl_follows_settings() -> None:
    s = make_settings(access_token_ttl_minutes=1, refresh_token_ttl_days=2)
    c = decode_token(create_access_token(USER_ID, s), "access", s)
    assert c.exp - c.iat == 60
    r = decode_token(create_refresh_token(USER_ID, s).token, "refresh", s)
    assert r.exp - r.iat == 2 * 86400


@pytest.mark.parametrize("bad_id", ["", "abc", "0123456789ABCDEF01234567", "0123456789abcdef0123456:", "x" * 24])
def test_create_refuses_non_objectid_subject(bad_id: str) -> None:
    with pytest.raises(ValueError):
        create_access_token(bad_id, SETTINGS)
    with pytest.raises(ValueError):
        create_refresh_token(bad_id, SETTINGS)


# ------------------------------------------------------------------ expiry


def test_expired_access_rejected() -> None:
    tok = create_access_token(USER_ID, SETTINGS, now=utcnow() - timedelta(minutes=16))
    assert rejected(tok)


def test_expired_refresh_rejected() -> None:
    tok = create_refresh_token(USER_ID, SETTINGS, now=utcnow() - timedelta(days=8)).token
    assert rejected(tok, "refresh")


def test_exp_in_the_past_by_one_second_rejected_no_leeway() -> None:
    now = int(time.time())
    assert rejected(hs256(claims(iat=now - 100, exp=now - 1)))


def test_not_yet_expired_accepted() -> None:
    now = int(time.time())
    assert not rejected(hs256(claims(iat=now - 100, exp=now + 5)))


# ------------------------------------------------------------------ signature / secret


def test_wrong_secret_rejected() -> None:
    assert rejected(hs256(claims(), secret=OTHER_SECRET))
    other = make_settings(jwt_secret=OTHER_SECRET)
    assert rejected(create_access_token(USER_ID, other))


def test_tampered_signature_rejected() -> None:
    tok = create_access_token(USER_ID, SETTINGS)
    head, body, sig = tok.split(".")
    mid = len(sig) // 2
    flipped = sig[:mid] + ("A" if sig[mid] != "A" else "B") + sig[mid + 1 :]
    assert rejected(f"{head}.{body}.{flipped}")
    assert rejected(f"{head}.{body}.")


def test_tampered_payload_rejected() -> None:
    tok = create_access_token(USER_ID, SETTINGS)
    head, _, sig = tok.split(".")
    forged_body = b64(claims(sub="fedcba9876543210fedcba98"))
    assert rejected(f"{head}.{forged_body}.{sig}")


# ------------------------------------------------------------------ algorithm


@pytest.mark.parametrize("alg", ["none", "None", "NONE"])
def test_alg_none_rejected(alg: str) -> None:
    tok = f"{b64({'alg': alg, 'typ': 'JWT'})}.{b64(claims())}."
    assert rejected(tok)


@pytest.mark.parametrize("alg", ["HS384", "HS512"])
def test_other_hmac_algorithms_rejected_even_with_right_secret(alg: str) -> None:
    assert rejected(jwt.encode(claims(), TEST_SECRET, algorithm=alg))


def test_rs256_rejected() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    assert rejected(jwt.encode(claims(), pem, algorithm="RS256"))


def test_rs256_header_with_hmac_signature_rejected() -> None:
    # Header claims RS256, but the signature is an HMAC with our secret (alg confusion).
    good = hs256(claims())
    _, body, sig = good.split(".")
    assert rejected(f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{body}.{sig}")


# ------------------------------------------------------------------ type confusion


def test_refresh_token_not_accepted_as_access() -> None:
    assert rejected(create_refresh_token(USER_ID, SETTINGS).token, "access")


def test_access_token_not_accepted_as_refresh() -> None:
    assert rejected(create_access_token(USER_ID, SETTINGS), "refresh")


@pytest.mark.parametrize("typ", ["Access", "ACCESS", "", "id", None, 1])
def test_bad_type_claim_rejected(typ) -> None:
    assert rejected(hs256(claims(type=typ)))


# ------------------------------------------------------------------ claims


@pytest.mark.parametrize("missing", ["sub", "type", "iat", "exp"])
def test_missing_required_access_claim_rejected(missing: str) -> None:
    assert rejected(hs256(claims(**{missing: ...})))


@pytest.mark.parametrize("missing", ["sub", "type", "iat", "exp", "jti"])
def test_missing_required_refresh_claim_rejected(missing: str) -> None:
    assert rejected(hs256(claims("refresh", **{missing: ...})), "refresh")


@pytest.mark.parametrize(
    "sub", [123, "", "abc", "0123456789ABCDEF01234567", "0123456789abcdef0123456:", "u:1", ["x"], None]
)
def test_bad_subject_rejected(sub) -> None:
    assert rejected(hs256(claims(sub=sub)))


@pytest.mark.parametrize("jti", ["", "short", "Z" * 32, 123, None, "0123456789ABCDEF0123456789ABCDEF"])
def test_bad_jti_rejected(jti) -> None:
    assert rejected(hs256(claims("refresh", jti=jti)), "refresh")


@pytest.mark.parametrize("field", ["iat", "exp"])
@pytest.mark.parametrize("value", ["1700000000", 1.5e10, True])
def test_non_integer_times_rejected(field: str, value) -> None:
    assert rejected(hs256(claims(**{field: value})))


def test_exp_not_after_iat_rejected() -> None:
    now = int(time.time())
    assert rejected(hs256(claims(iat=now + 50, exp=now + 50)))


# ------------------------------------------------------------------ garbage input


@pytest.mark.parametrize(
    "tok",
    ["", "garbage", "a.b", "a.b.c", "....", "Bearer x", "é.é.é", "eyJhbGciOiJIUzI1NiJ9.e30."],
)
def test_garbage_rejected(tok: str) -> None:
    assert rejected(tok)


def test_oversized_token_rejected_without_parsing() -> None:
    tok = hs256(claims(pad="x" * 5000))
    assert len(tok) > 4096
    assert rejected(tok)
