# TravelBot

An AI travel assistant. Add your stops and dates, and it plans a day-by-day itinerary.
Then keep chatting: ask about food, transport, budget or weather. There's no login: open
the page and start asking.

Answers stream in live. Prices come in the destination's currency plus your home
currency, and times are given in local time.

## How it works

```
Browser ──► FastAPI backend ──► LangGraph agent ──► MongoDB Atlas (travel tips)
   ▲               │                   │
   └── streamed ◄──┘                   └──► live tools: web search, weather, currency
       answer
```

### The agent (LangGraph + Groq)

Each question goes through a small graph of steps. The model is `openai/gpt-oss-120b` on Groq.

1. **plan**: works out what you're asking, the destination, and whether live data is needed.
2. **retrieve**: searches the stored travel tips in MongoDB Atlas.
3. **grade**: checks whether the tips actually answer the question.
   - If they don't, **rewrite** the search query and try again (up to 2 times).
   - If they still don't, or you asked something current, run a **web search** (Tavily)
     plus **weather** (Open-Meteo) or **currency** (Frankfurter) when relevant.
4. **generate**: writes the answer from that context. Its tokens are streamed to the browser.

Conversation history is kept per chat thread, so follow-up questions have context.

### The RAG approach (MongoDB Atlas)

The knowledge base is about 10,000 Reddit travel posts, including their comments, where
most of the real advice is.

- **Tagging**: each post is tagged with real destinations (spaCy + a local LLM) and with topics
  (food, transport, budget, ...).
- **Chunking**: posts are split into post, summary and comment chunks, so one good comment can be
  found on its own. Each chunk keeps the post title and tags for context.
- **Embedding**: chunks are embedded with `nomic-embed-text` (Ollama) and stored in Atlas as
  compact float32 vectors.
- **Hybrid search**: at question time, Atlas **Vector Search** (meaning) and Atlas **Search**
  (keywords) run together, filtered by destination. The two result lists are merged with
  reciprocal rank fusion and grouped by post.

## Project layout

| Folder | What's in it |
|---|---|
| `frontend/` | Static web UI (HTML/CSS/JS), served by nginx, which also proxies `/api` to the backend |
| `backend/` | FastAPI app: `/chat`, `/chat/stream` (Server-Sent Events), `/health`, per-IP rate limit |
| `agent/` | LangGraph agent and tools |
| `data/` | Hybrid search over Atlas (`search_stored_tips`) |
| `preprocessing/` | Data pipeline: tag, chunk and embed the Reddit posts |
| `mongo/` | Loads the processed data and search indexes into Atlas |

## Run it

You need Docker, a Groq API key and a MongoDB Atlas connection string.

```bash
cp .env.example .env        # fill in GROQ_API_KEY and MONGODB_URI
docker compose up --build
```

This starts the backend, the frontend and Ollama (for query embeddings). Open http://localhost:8501.

API details: [`backend/CONTRACT.md`](backend/CONTRACT.md). More on running and tests:
[`backend/README.md`](backend/README.md).
