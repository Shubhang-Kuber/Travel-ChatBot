"""Every request/response schema: valid accepted, each invalid field rejected."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)
from backend.schemas.chat import ChatRequest, ChatResponse
from backend.schemas.common import ErrorBody, ErrorResponse, HealthResponse

pytestmark = pytest.mark.unit

PW = "correct horse battery"


def invalid(model, **data) -> list[dict]:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    return exc.value.errors()


# ------------------------------------------------------------------ ChatRequest


def test_chat_minimal_valid() -> None:
    req = ChatRequest.model_validate({"message": "hi"})
    assert req.message == "hi" and req.thread_id is None


def test_chat_message_is_stripped() -> None:
    assert ChatRequest.model_validate({"message": "  \n hi there \t"}).message == "hi there"


def test_chat_message_limit_applies_after_strip() -> None:
    assert len(ChatRequest.model_validate({"message": "  " + "x" * 4000 + "  "}).message) == 4000


@pytest.mark.parametrize("msg", ["", "   ", "\n\t", "x" * 4001])
def test_chat_bad_message_rejected(msg: str) -> None:
    invalid(ChatRequest, message=msg)


@pytest.mark.parametrize("msg", [None, 123, ["hi"], {"text": "hi"}, True])
def test_chat_non_string_message_rejected(msg) -> None:
    invalid(ChatRequest, message=msg)


def test_chat_missing_message_rejected() -> None:
    invalid(ChatRequest, thread_id="t1")


@pytest.mark.parametrize("tid", ["a", "trip-lisbon-1", "A_b-9", "x" * 64, "0" * 32])
def test_chat_thread_id_valid(tid: str) -> None:
    assert ChatRequest.model_validate({"message": "hi", "thread_id": tid}).thread_id == tid


@pytest.mark.parametrize(
    "tid",
    ["", "x" * 65, "a:b", "a b", "a/b", "../etc", "café", "a.b", "abc\n", "\nabc", "a\x00b", "$where", "{}"],
)
def test_chat_thread_id_bad_pattern_rejected(tid: str) -> None:
    invalid(ChatRequest, message="hi", thread_id=tid)


@pytest.mark.parametrize("tid", [123, ["a"], {"$ne": ""}])
def test_chat_thread_id_non_string_rejected(tid) -> None:
    invalid(ChatRequest, message="hi", thread_id=tid)


def test_chat_explicit_null_thread_id_means_new_thread() -> None:
    assert ChatRequest.model_validate({"message": "hi", "thread_id": None}).thread_id is None


@pytest.mark.parametrize("extra", ["threadid", "user_id", "role"])
def test_chat_unknown_fields_rejected(extra: str) -> None:
    errors = invalid(ChatRequest, message="hi", **{extra: "x"})
    assert errors[0]["type"] == "extra_forbidden"


def test_chat_response_shape() -> None:
    assert ChatResponse(response="r", thread_id="t").model_dump() == {"response": "r", "thread_id": "t"}


# ------------------------------------------------------------------ Signup / Login


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("alice@example.com", "alice@example.com"),
        ("  Alice@Example.COM  ", "alice@example.com"),
        ("BOB.Smith+Trips@Mail.Example.org", "bob.smith+trips@mail.example.org"),
    ],
)
@pytest.mark.parametrize("model", [SignupRequest, LoginRequest])
def test_email_normalized(model, raw: str, normalized: str) -> None:
    assert model.model_validate({"email": raw, "password": PW}).email == normalized


@pytest.mark.parametrize(
    "email", ["", "   ", "notanemail", "a@", "@example.com", "a@@example.com", "a b@example.com", "a@example", 42, None]
)
@pytest.mark.parametrize("model", [SignupRequest, LoginRequest])
def test_bad_email_rejected(model, email) -> None:
    invalid(model, email=email, password=PW)


@pytest.mark.parametrize("model", [SignupRequest, LoginRequest])
def test_missing_fields_rejected(model) -> None:
    invalid(model, email="a@example.com")
    invalid(model, password=PW)
    invalid(model)


@pytest.mark.parametrize("model", [SignupRequest, LoginRequest])
@pytest.mark.parametrize("extra", ["username", "is_admin", "role"])
def test_auth_unknown_fields_rejected(model, extra: str) -> None:
    invalid(model, email="a@example.com", password=PW, **{extra: True})


@pytest.mark.parametrize(
    "pw",
    [
        "a" * 8,
        "a" * 72,
        "€" * 24,  # 72 bytes
        "é" * 4,  # 8 bytes
        "spaces are fine ok",
    ],
)
def test_signup_password_policy_accepts(pw: str) -> None:
    assert SignupRequest.model_validate({"email": "a@example.com", "password": pw}).password == pw


@pytest.mark.parametrize(
    "pw",
    [
        "",
        "a" * 7,
        "a" * 73,
        "€" * 25,  # 25 chars, 75 bytes
        "é" * 3,  # 6 bytes
        "abcd\x00efgh",
        12345678,
        None,
    ],
)
def test_signup_password_policy_rejects(pw) -> None:
    invalid(SignupRequest, email="a@example.com", password=pw)


@pytest.mark.parametrize("pw", ["x", "a" * 7, "a" * 73, "a" * 1024])
def test_login_has_no_signup_policy(pw: str) -> None:
    # Short/long passwords at login must reach the 401 path, not a distinct 422.
    assert LoginRequest.model_validate({"email": "a@example.com", "password": pw}).password == pw


@pytest.mark.parametrize("pw", ["", "a" * 1025, 123])
def test_login_password_bounds(pw) -> None:
    invalid(LoginRequest, email="a@example.com", password=pw)


def test_password_not_stripped() -> None:
    assert SignupRequest.model_validate({"email": "a@example.com", "password": "  padded pw  "}).password == "  padded pw  "


# ------------------------------------------------------------------ Refresh / Logout


@pytest.mark.parametrize("model", [RefreshRequest, LogoutRequest])
def test_refresh_token_body_valid(model) -> None:
    assert model.model_validate({"refresh_token": "abc.def.ghi"}).refresh_token == "abc.def.ghi"


@pytest.mark.parametrize("model", [RefreshRequest, LogoutRequest])
@pytest.mark.parametrize("tok", ["", "x" * 4097, None, 123, ["a"]])
def test_refresh_token_body_bad_rejected(model, tok) -> None:
    invalid(model, refresh_token=tok)


@pytest.mark.parametrize("model", [RefreshRequest, LogoutRequest])
def test_refresh_token_body_missing_or_extra_rejected(model) -> None:
    invalid(model)
    invalid(model, refresh_token="a.b.c", access_token="x")


# ------------------------------------------------------------------ responses


def test_token_response_shape() -> None:
    r = TokenResponse(access_token="a", refresh_token="r", expires_in=900)
    assert r.model_dump() == {"access_token": "a", "refresh_token": "r", "token_type": "bearer", "expires_in": 900}
    with pytest.raises(ValidationError):
        TokenResponse(access_token="a", refresh_token="r", token_type="mac", expires_in=900)


def test_user_response_has_no_secret_fields() -> None:
    r = UserResponse(id="0" * 24, email="a@example.com", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert set(r.model_dump()) == {"id", "email", "created_at"}
    assert set(UserResponse.model_fields) == {"id", "email", "created_at"}


def test_error_envelope_shape() -> None:
    e = ErrorResponse(error=ErrorBody(code="not_found", message="Not Found"))
    assert e.model_dump() == {"error": {"code": "not_found", "message": "Not Found", "details": None}}


@pytest.mark.parametrize("status, mongo", [("ok", "up"), ("degraded", "down")])
def test_health_response_valid(status: str, mongo: str) -> None:
    assert HealthResponse(status=status, mongo=mongo).model_dump() == {"status": status, "mongo": mongo}


def test_health_response_rejects_other_values() -> None:
    with pytest.raises(ValidationError):
        HealthResponse(status="fine", mongo="up")
    with pytest.raises(ValidationError):
        HealthResponse(status="ok", mongo="maybe")
