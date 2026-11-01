"""Request/response bodies for /chat."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

THREAD_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
# Browser values: navigator.language (BCP 47, e.g. "en-IN") and the IANA timezone ("Asia/Kolkata").
LOCALE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8}){0,4}$"
TIMEZONE_PATTERN = r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$"
MAX_MESSAGE_CHARS = 4000

ThreadId = Annotated[str, StringConstraints(pattern=THREAD_ID_PATTERN)]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)]
    thread_id: ThreadId | None = None
    locale: Annotated[str, StringConstraints(pattern=LOCALE_PATTERN, max_length=35)] | None = None
    timezone: Annotated[str, StringConstraints(pattern=TIMEZONE_PATTERN, max_length=64)] | None = None


class ChatResponse(BaseModel):
    response: str
    thread_id: str
