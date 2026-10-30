from typing import Any, Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    query: str
    destination: str | None
    retrieved_docs: list[dict]
    web_results: list[dict[str, Any]]
    weather_results: dict[str, Any] | None
    currency_results: dict[str, Any] | None
    needs_live_info: bool
    needs_weather: bool
    weather_location: str | None
    weather_dates: list[str]
    needs_currency: bool
    currency_amount: float | None
    from_currency: str | None
    to_currency: str | None
    retry_count: int
    retrieval_is_relevant: bool
    itinerary: dict[str, Any] | None