from typing import Any, Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    query: str
    destination: str | None
    retrieved_docs: list[dict]
    web_results: list[dict[str, Any]]
    retry_count: int
    retrieval_is_relevant: bool
    itinerary: dict[str, Any] | None