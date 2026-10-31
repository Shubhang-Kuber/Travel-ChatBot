# Plan: swap Gemini for Groq (single provider, no fallback chain)

## Context
Gemini free tier keeps failing (timeouts, overloaded, quota exhausted), and when it does work a turn takes ~15s. Each turn makes 3–5 sequential LLM calls (`plan`, `grade`, optional `rewrite`, `generate` in `agent/graph.py`). `gemini-2.5-flash` also spends time "thinking" on every call, so latency adds up.

Groq runs Llama/gpt-oss models at hundreds of tokens per second. Expect the LLM part of a turn to take a few seconds, versus ~15s now. It supports structured output, which the graph relies on. The change is a one-provider swap: the architecture stays the same and there are no fallbacks.

Why not the alternatives:
- Ollama Cloud free: it works, but it has hourly and daily caps and is slower than Groq.
- Local `qwen2.5:7b`: around 10–40s per call on a Mac, times 3–5 calls per turn, which is worse than Gemini.

## Changes

### 1. `agent/llm.py`
Replace `ChatGoogleGenerativeAI` with `ChatGroq`:
```python
from langchain_groq import ChatGroq

def get_llm() -> BaseChatModel:
	"""Create the Groq chat model used by the LangGraph agent."""
	api_key = os.getenv("GROQ_API_KEY")
	if not api_key:
		raise RuntimeError("GROQ_API_KEY must be set")
	return ChatGroq(
		model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
		api_key=api_key,
		temperature=0.2,
		timeout=30,
		max_retries=2,
	)
```
`agent/graph.py` does not change, apart from the docstring at line 337 ("Ask Gemini…" becomes "Ask the LLM…").

### 2. `requirements.txt`
Add `langchain-groq`, pinned to a version compatible with `langchain-core==1.7.3`. Remove `langchain-google-genai` and `google-genai` if nothing else imports them (grep first).

### 3. `.env.example` and `.env`
Replace the Gemini lines with:
```
GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
```
Get a free key at console.groq.com. Put the real key only in `.env`.

**Security:** your working copy of `.env.example`, which git tracks, currently holds a real `JWT_SECRET`, Atlas credentials, a Gemini key and a Tavily key. Restore the blank placeholders there before committing, and keep the real values in `.env`.

### 4. If Groq limits are hit
The `grade` node sends whole documents, which uses up tokens. If you see 429 "tokens per day", set `GROQ_MODEL=llama-3.1-8b-instant` or `openai/gpt-oss-20b`; both have higher free limits. This is an env change only, with no code change. Current limits are listed at console.groq.com/settings/limits.

## Verification
1. `pip install -r requirements.txt && pip check`
2. `docker compose up --build`, log in, send a trip question. Expect a full itinerary (no raw JSON), with total turn time well under the current ~15s.
3. `pytest backend/tests/unit backend/tests/integration` should stay green. Tests use the mock or fake LLM, so nothing there changes.
