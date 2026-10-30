import json
import os
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agent.llm import get_llm
from agent.state import AgentState


SearchFunction = Callable[[str, str | None], list[dict[str, Any]]]

MAX_RETRIES = 2
# LangGraph skips LLM calls carrying this tag in stream_mode="messages".
NOSTREAM = {"tags": ["nostream"]}


def _get_json(url: str, request: Request | None = None) -> dict[str, Any]:
    """Fetch and decode a JSON API response."""

    try:
        with urlopen(request or url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as error:
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
        f"?name={location}&count=1&language=en&format=json"
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
        f"?amount={amount}&from={source}&to={target}"
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


def build_itinerary(structured_plan: dict[str, Any]) -> dict[str, Any]:
    """Normalize a generated itinerary into the backend response shape."""

    destination = structured_plan.get("destination")
    days = structured_plan.get("days", [])
    lines = [f"Itinerary for {destination}" if destination else "Itinerary"]

    for day in days:
        day_number = day.get("day", len(lines))
        title = day.get("title", "")
        activities = day.get("activities", [])
        lines.append(f"\nDay {day_number}: {title}".rstrip())
        lines.extend(f"- {activity}" for activity in activities)

    notes = structured_plan.get("notes", [])
    if notes:
        lines.append("\nNotes")
        lines.extend(f"- {note}" for note in notes)

    return {
        **structured_plan,
        "text": "\n".join(lines),
    }


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


class ItineraryPlan(BaseModel):
    """Structured itinerary generated from the available context."""

    destination: str | None = None
    days: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


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
            "itinerary": None,
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

        return {"web_results": live_search(search_query)}

    def supplemental_tools_node(state: AgentState) -> dict[str, Any]:
        """Run requested weather and currency tools after live fallback."""

        updates: dict[str, Any] = {
            "weather_results": None,
            "currency_results": None,
        }

        if state.get("needs_weather") and state.get("weather_location"):
            updates["weather_results"] = get_weather(
                state["weather_location"],
                state.get("weather_dates", []),
            )

        currency_values = (
            state.get("currency_amount"),
            state.get("from_currency"),
            state.get("to_currency"),
        )
        if state.get("needs_currency") and all(value is not None for value in currency_values):
            updates["currency_results"] = convert_currency(
                state["currency_amount"],
                state["from_currency"],
                state["to_currency"],
            )

        return updates

    def grade_node(state: AgentState) -> dict[str, Any]:
        """Ask Gemini whether the retrieved documents are relevant."""

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
                    content=json.dumps(
                        {
                            "query": query,
                            "stored_documents": documents,
                        },
                        default=str,
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
        """Generate and normalize the final itinerary from available context."""

        documents = (
            state.get("retrieved_docs", [])
            if state.get("retrieval_is_relevant", False)
            else []
        )
        web_results = state.get("web_results", [])
        itinerary_generator = model.with_structured_output(ItineraryPlan)

        structured_plan = itinerary_generator.invoke(
            [
                SystemMessage(
                    content=(
                        "You are a travel planning assistant. "
                        "Create a practical itinerary using the supplied context. "
                        "Do not invent facts. Put uncertain information in notes."
                    )
                ),
                *state["messages"][-6:],
                HumanMessage(
                    content=json.dumps(
                        {
                            "stored_travel_tips": documents,
                            "web_results": web_results,
                            "weather": state.get("weather_results"),
                            "currency": state.get("currency_results"),
                        },
                        default=str,
                    )
                ),
            ],
            # Raw structured-output JSON must not reach the user; the formatted
            # AIMessage this node returns is what gets streamed.
            config=NOSTREAM,
        )

        itinerary = build_itinerary(structured_plan.model_dump())
        answer = itinerary["text"]

        return {
            "itinerary": itinerary,
            "messages": [
                AIMessage(content=answer),
            ],
        }

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