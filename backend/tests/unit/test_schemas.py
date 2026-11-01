"""Every request/response schema: valid accepted, each invalid field rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.schemas.chat import ChatRequest, ChatResponse
from backend.schemas.common import ErrorBody, ErrorResponse, HealthResponse

pytestmark = pytest.mark.unit

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


# ------------------------------------------------------------------ responses


def test_error_envelope_shape() -> None:
    e = ErrorResponse(error=ErrorBody(code="not_found", message="Not Found"))
    assert e.model_dump() == {"error": {"code": "not_found", "message": "Not Found", "details": None}}


def test_health_response_valid() -> None:
    assert HealthResponse(status="ok").model_dump() == {"status": "ok"}


def test_health_response_rejects_other_values() -> None:
    with pytest.raises(ValidationError):
        HealthResponse(status="fine")


# ------------------------------------------------------------------ locale / timezone


@pytest.mark.parametrize("locale", ["en", "en-IN", "pt-BR", "zh-Hant-TW", "es-419"])
def test_chat_locale_valid(locale: str) -> None:
    assert ChatRequest.model_validate({"message": "hi", "locale": locale}).locale == locale


@pytest.mark.parametrize("locale", ["", "e", "en_IN", "en IN", "<script>", "x" * 36])
def test_chat_locale_invalid(locale: str) -> None:
    invalid(ChatRequest, message="hi", locale=locale)


@pytest.mark.parametrize("tz", ["UTC", "Asia/Kolkata", "America/Argentina/Buenos_Aires", "Etc/GMT+5"])
def test_chat_timezone_valid(tz: str) -> None:
    assert ChatRequest.model_validate({"message": "hi", "timezone": tz}).timezone == tz


@pytest.mark.parametrize("tz", ["", "/etc", "Asia//Kolkata", "a b", "Asia/Kolkata\nx", "x" * 65])
def test_chat_timezone_invalid(tz: str) -> None:
    invalid(ChatRequest, message="hi", timezone=tz)
