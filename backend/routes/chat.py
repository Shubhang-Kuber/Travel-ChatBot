"""POST /chat and POST /chat/stream: validate, delegate to the graph adapter, serialize. Nothing else."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.config import Settings
from backend.dependencies import CurrentUser, chat_rate_limit, get_current_user, get_graph, get_settings
from backend.graph import TravelGraph, run_chat
from backend.schemas.chat import ChatRequest, ChatResponse
from backend.schemas.common import ErrorResponse
from backend.streaming import SSE_HEADERS, SSE_MEDIA_TYPE, stream_chat_events

router = APIRouter(tags=["chat"])

_ERRORS = {code: {"model": ErrorResponse} for code in (401, 422, 429)}


@router.post(
    "/chat",
    response_model=ChatResponse,
    dependencies=[Depends(chat_rate_limit)],
    responses={**_ERRORS, 502: {"model": ErrorResponse}},
)
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    graph: TravelGraph = Depends(get_graph),
) -> ChatResponse:
    thread_id = body.thread_id or uuid4().hex
    reply = await run_chat(graph, user.user_id, thread_id, body.message)
    return ChatResponse(response=reply, thread_id=thread_id)


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    dependencies=[Depends(chat_rate_limit)],
    responses={200: {"content": {SSE_MEDIA_TYPE: {}}, "description": "SSE: token*, then done or error"}, **_ERRORS},
)
async def chat_stream(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    graph: TravelGraph = Depends(get_graph),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    thread_id = body.thread_id or uuid4().hex
    events = stream_chat_events(graph, settings.stream_node, user.user_id, thread_id, body.message)
    return StreamingResponse(events, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)
