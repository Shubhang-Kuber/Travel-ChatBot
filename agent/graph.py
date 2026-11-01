import json
import logging
import os
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agent.llm import get_llm
from agent.state import AgentState


log = logging.getLogger(__name__)

SearchFunction = Callable[[str, str | None], list[dict[str, Any]]]

MAX_RETRIES = 2
# The grader only needs a taste of each chunk; full text goes to generate alone.
GRADE_SNIPPET_CHARS = 300

GENERATE_PROMPT = """You are a travel planning assistant. Answer using the supplied context.
Do not invent facts. Mention uncertain or missing information in the Notes.

Write plain text. You may use **bold** for titles and key facts. Never output JSON,
code blocks, tables, or other markdown symbols such as # or |.

Money and time:
- Give every price in the destination's local currency, followed by the approximate
  amount in the traveller's home currency, e.g. "EUR 25 (about INR 2,300)". Never give
  prices in USD only, unless USD is the local or the home currency.
- Infer the home currency from the traveller's locale and timezone when they are given.
- Give opening hours and times in the destination's local time, and use the
  traveller's usual date and time format.

For a trip plan use exactly this layout:
Itinerary for <destination>

Day 1: <short title>
<one-line summary>
- <activity>
- <activity>

Notes
- <note>

For any other question (weather, currency, a single tip), answer directly in short
sentences or "- " bullet lines."""


def _get_json(url: str, request: Request | None = None) -> dict[str, Any]:
    """Fetch and decode a JSON API response."""

    try:
        with urlopen(request or url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, HTTPException, TimeoutError, ValueError) as error:
        raise RuntimeError(f"Request failed: {url}") from error


def live_search(query: str) -> list[dict[str, Any]]:
    """Search the live web through Tavily when a key is configured."""

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return []

    request = Request(
        "https://api.tavily.com/search",
        data=json.dumps(
            {
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    payload = _get_json("https://api.tavily.com/search", request)

    return [
        {
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "content": result.get("content", ""),
        }
        for result in payload.get("results", [])
    ]


def get_weather(location: str, dates: list[str] | None = None) -> dict[str, Any]:
    """Get a forecast from Open-Meteo for a location and optional dates."""

    geocode_url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={quote(location)}&count=1&language=en&format=json"
    )
    geocoded = _get_json(geocode_url)
    locations = geocoded.get("results", [])
    if not locations:
        return {"location": location, "error": "Location not found"}

    place = locations[0]
    forecast_url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={place['latitude']}&longitude={place['longitude']}"
        "&daily=temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,weather_code"
        "&forecast_days=16&timezone=auto"
    )
    forecast = _get_json(forecast_url)
    daily = forecast.get("daily", {})
    requested_dates = set(dates or daily.get("time", []))

    forecast_days = []
    for index, date in enumerate(daily.get("time", [])):
        if date not in requested_dates:
            continue
        forecast_days.append(
            {
                "date": date,
                "temperature_max": daily["temperature_2m_max"][index],
                "temperature_min": daily["temperature_2m_min"][index],
                "precipitation_probability": daily[
                    "precipitation_probability_max"
                ][index],
                "weather_code": daily["weather_code"][index],
            }
        )

    return {
        "location": place.get("name", location),
        "country": place.get("country"),
        "timezone": forecast.get("timezone"),
        "forecast": forecast_days,
    }


def convert_currency(
    amount: float,
    from_currency: str,
    to_currency: str,
) -> dict[str, Any]:
    """Convert currency using the latest Frankfurter exchange rate."""

    source = from_currency.upper()
    target = to_currency.upper()
    url = (
        "https://api.frankfurter.app/latest"
        f"?amount={amount}&from={quote(source)}&to={quote(target)}"
    )
    result = _get_json(url)
    rates = result.get("rates", {})

    return {
        "amount": amount,
        "from_currency": source,
        "to_currency": target,
        "converted_amount": rates.get(target),
        "rate_date": result.get("date"),
    }


def _docs_text(documents: list[dict[str, Any]], snippet_chars: int | None = None) -> str:
    """Render stored posts as plain text (plain text in keeps JSON out of the reply)."""

    lines = []
    for number, doc in enumerate(documents, start=1):
        lines.append(f"[{number}] {doc.get('title', '')}".rstrip())
        for chunk in doc.get("chunks", []):
            text = " ".join(str(chunk.get("text", "")).split())
            if snippet_chars:
                text = text[:snippet_chars]
            if text:
                lines.append(f"- {text}")
    return "\n".join(lines)


def _context_text(state: AgentState, documents: list[dict[str, Any]]) -> str:
    """Everything generate may use, as readable sections instead of a JSON dump."""

    sections = []
    if documents:
        sections.append("Stored travel tips:\n" + _docs_text(documents))

    web_results = state.get("web_results") or []
    if web_results:
        lines = [
            f"[{n}] {r.get('title', '')} ({r.get('url', '')})\n{r.get('content', '')}"
            for n, r in enumerate(web_results, start=1)
        ]
        sections.append("Web results:\n" + "\n".join(lines))

    weather = state.get("weather_results")
    if weather:
        if weather.get("error"):
            sections.append(f"Weather: unavailable for {weather.get('location')} ({weather['error']})")
        else:
            lines = [
                f"- {d['date']}: {d['temperature_min']} to {d['temperature_max']} C, "
                f"rain chance {d['precipitation_probability']}%"
                for d in weather.get("forecast", [])
            ]
            place = ", ".join(p for p in (weather.get("location"), weather.get("country")) if p)
            sections.append(f"Weather forecast for {place}:\n" + ("\n".join(lines) or "- no days in range"))

    currency = state.get("currency_results")
    if currency:
        if currency.get("error"):
            sections.append(f"Currency: unavailable ({currency['error']})")
        else:
            sections.append(
                f"Currency: {currency['amount']} {currency['from_currency']} = "
                f"{currency['converted_amount']} {currency['to_currency']} "
                f"(rate date {currency['rate_date']})"
            )

    return "\n\n".join(sections) or "No extra context was found."


class TravelPlan(BaseModel):
    """Normalized travel request extracted from the user's message."""

    query: str = Field(description="The travel information the agent needs")
    destination: str | None = Field(
        default=None,
        description="The requested destination, if one was provided",
    )
    needs_live_info: bool = Field(
        default=False,
        description="Whether current web information is needed",
    )
    needs_weather: bool = False
    weather_location: str | None = None
    weather_dates: list[str] = Field(default_factory=list)
    needs_currency: bool = False
    currency_amount: float | None = None
    from_currency: str | None = None
    to_currency: str | None = None


class RelevanceGrade(BaseModel):
    """Result of checking whether retrieved documents are useful."""

    is_relevant: bool = Field(
        description="Whether the retrieved documents answer the user's request"
    )


def create_graph(
    search_stored_tips: SearchFunction,
    llm: BaseChatModel | None = None,
    checkpointer: Any | None = None,
):
    """Build and compile the travel assistant graph.

    Pass a shared MongoDB checkpointer in production so conversation state is
    available to every worker or container.
    """

    model = llm or get_llm()

    def plan_node(state: AgentState) -> dict[str, Any]:
        """Extract a clean request while preserving recent conversation context."""

        planner = model.with_structured_output(TravelPlan)

        plan = planner.invoke(
            [
                SystemMessage(
                    content=(
                        "Extract the user's travel research request. "
                        "Return a concise search query and the destination "
                        "if it is explicitly mentioned. Set needs_live_info "
                        "for current web facts, needs_weather for forecasts, "
                        "and needs_currency for exchange-rate requests."
                    )
                ),
                *state["messages"][-6:],
            ]
        )

        destination = plan.destination or state.get("destination")

        return {
            "query": plan.query,
            "destination": destination,
            "retrieved_docs": [],
            "web_results": [],
            "weather_results": None,
            "currency_results": None,
            "needs_live_info": (
                plan.needs_live_info
                or plan.needs_weather
                or plan.needs_currency
            ),
            "needs_weather": plan.needs_weather,
            "weather_location": plan.weather_location or destination,
            "weather_dates": plan.weather_dates,
            "needs_currency": plan.needs_currency,
            "currency_amount": plan.currency_amount,
            "from_currency": plan.from_currency,
            "to_currency": plan.to_currency,
            "retry_count": 0,
            "retrieval_is_relevant": False,
        }

    def retrieve_node(state: AgentState) -> dict[str, Any]:
        """Search the stored travel knowledge base."""

        query = state.get("query", "")
        destination = state.get("destination")

        documents = search_stored_tips(query, destination)

        return {"retrieved_docs": documents}

    def web_search_node(state: AgentState) -> dict[str, Any]:
        """Run one live web search after stored retrieval is insufficient."""

        query = state.get("query", "")
        destination = state.get("destination")
        search_query = f"{destination}: {query}" if destination else query

        try:
            return {"web_results": live_search(search_query)}
        except RuntimeError:
            # A flaky free API must not kill the turn; generate answers without it.
            log.warning("Web search failed", exc_info=True)
            return {"web_results": []}

    def supplemental_tools_node(state: AgentState) -> dict[str, Any]:
        """Run requested weather and currency tools after live fallback."""

        updates: dict[str, Any] = {
            "weather_results": None,
            "currency_results": None,
        }

        if state.get("needs_weather") and state.get("weather_location"):
            try:
                updates["weather_results"] = get_weather(
                    state["weather_location"],
                    state.get("weather_dates", []),
                )
            except RuntimeError:
                log.warning("Weather lookup failed", exc_info=True)
                updates["weather_results"] = {
                    "location": state["weather_location"],
                    "error": "weather service unreachable",
                }

        currency_values = (
            state.get("currency_amount"),
            state.get("from_currency"),
            state.get("to_currency"),
        )
        if state.get("needs_currency") and all(value is not None for value in currency_values):
            try:
                updates["currency_results"] = convert_currency(
                    state["currency_amount"],
                    state["from_currency"],
                    state["to_currency"],
                )
            except RuntimeError:
                log.warning("Currency lookup failed", exc_info=True)
                updates["currency_results"] = {"error": "currency service unreachable"}

        return updates

    def grade_node(state: AgentState) -> dict[str, Any]:
        """Ask the LLM whether the retrieved documents are relevant."""

        documents = state.get("retrieved_docs", [])
        query = state.get("query", "")

        grader = model.with_structured_output(RelevanceGrade)

        grade = grader.invoke(
            [
                SystemMessage(
                    content=(
                        "Decide whether the retrieved travel documents are "
                        "useful for answering the user's query. "
                        "Return true only when they contain relevant information."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Query: {query}\n\nRetrieved documents:\n"
                        f"{_docs_text(documents, GRADE_SNIPPET_CHARS) or 'none'}"
                    )
                ),
            ]
        )

        return {
            "retrieval_is_relevant": grade.is_relevant,
        }

    def rewrite_node(state: AgentState) -> dict[str, Any]:
        """Rewrite a weak search query before trying retrieval again."""

        query = state.get("query", "")
        destination = state.get("destination")

        response = model.invoke(
            [
                SystemMessage(
                    content=(
                        "Rewrite the travel search query to make it more "
                        "specific and useful for retrieving travel advice. "
                        "Return only the rewritten query."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Original query: {query}\n"
                        f"Destination: {destination or 'not specified'}"
                    )
                ),
            ]
        )

        return {
            "query": str(response.content).strip(),
            "retry_count": state.get("retry_count", 0) + 1,
        }

    def route_after_grade(state: AgentState) -> str:
        """Choose generation, another retrieval attempt, or live fallback."""

        relevant = state.get("retrieval_is_relevant", False)
        needs_live_info = (
            state.get("needs_live_info", False)
            or state.get("needs_weather", False)
            or state.get("needs_currency", False)
        )

        if relevant and not needs_live_info:
            return "generate"

        if not relevant and state.get("retry_count", 0) < MAX_RETRIES:
            return "rewrite"

        return "web_search"

    def generate_node(state: AgentState) -> dict[str, Any]:
        """Write the final answer as plain text; its tokens stream to the user live.

        Plain-text output (no structured output) is what makes token streaming safe:
        there is no JSON to hide. The context also goes in as plain text, so the model
        has no JSON to copy.
        """

        documents = (
            state.get("retrieved_docs", [])
            if state.get("retrieval_is_relevant", False)
            else []
        )

        prompt = GENERATE_PROMPT
        locale, timezone = state.get("user_locale"), state.get("user_timezone")
        if locale or timezone:
            prompt += f"\n\nTraveller: locale {locale or 'unknown'}, timezone {timezone or 'unknown'}."

        response = model.invoke(
            [
                SystemMessage(content=prompt),
                *state["messages"][-6:],
                HumanMessage(content="Context:\n\n" + _context_text(state, documents)),
            ]
        )

        # Return the streamed message itself: LangGraph dedupes it by id, so the user
        # does not receive the answer twice.
        return {"messages": [response]}

    builder = StateGraph(AgentState)

    builder.add_node("plan", plan_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("web_search", web_search_node)
    builder.add_node("supplemental_tools", supplemental_tools_node)
    builder.add_node("grade", grade_node)
    builder.add_node("rewrite", rewrite_node)
    builder.add_node("generate", generate_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "grade")

    builder.add_conditional_edges(
        "grade",
        route_after_grade,
        {
            "generate": "generate",
            "rewrite": "rewrite",
            "web_search": "web_search",
        },
    )

    builder.add_edge("rewrite", "retrieve")
    builder.add_edge("web_search", "supplemental_tools")
    builder.add_edge("supplemental_tools", "generate")
    builder.add_edge("generate", END)

    return builder.compile(checkpointer=checkpointer)


def _default_search_stored_tips(
    query: str,
    destination: str | None,
) -> list[dict[str, Any]]:
    """Load Person A's search implementation at graph invocation time."""

    try:
        from data.search import search_stored_tips
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Implement data.search.search_stored_tips before invoking graph"
        ) from error

    return search_stored_tips(query, destination)


# In-memory per-thread history (backend runs one uvicorn worker); lost on restart.
graph = create_graph(_default_search_stored_tips, checkpointer=InMemorySaver())