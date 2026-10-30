"""Request/response bodies for /chat."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

THREAD_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MAX_MESSAGE_CHARS = 4000

ThreadId = Annotated[str, StringConstraints(pattern=THREAD_ID_PATTERN)]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)]
    thread_id: ThreadId | None = None


class ChatResponse(BaseModel):
    response: str
    thread_id: str
