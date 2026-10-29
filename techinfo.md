# WanderChat Agentic Travel Assistant — Build Plan

A step-by-step plan to build an agentic, RAG-powered travel itinerary assistant.
Written for a beginner in MongoDB, Docker, and agentic AI — every phase includes
what to learn before you build it, not just what to build.

---

## 0. What you're building (one paragraph)

A chatbot that plans travel itineraries by combining a **MongoDB vector-search
knowledge base** (built from a public Reddit travel dataset) with a
**LangGraph agent** that decides when to search that knowledge base, when to
search the live web, and how to assemble a final itinerary. It runs against
**Gemini Flash** during development (free, reliable tool-calling) with a
**local Ollama model** as a toggleable offline mode. The whole thing is
containerized with **Docker Compose**.

**Tech stack:** Python, MongoDB (Atlas or local-in-Docker), LangGraph,
FastAPI, Gemini API + Ollama, Docker Compose, Streamlit (or React) frontend.

---

## 1. Prerequisites — install before you start

| Tool | Why | Install |
|---|---|---|
| Python 3.11+ | Everything backend | [python.org](https://www.python.org/downloads/) |
| Docker Desktop | Containers | [docker.com](https://www.docker.com/products/docker-desktop/) |
| Git + GitHub account | Version control, resume link | you likely have this already |
| MongoDB Compass (optional) | GUI to browse your DB | [mongodb.com/products/compass](https://www.mongodb.com/products/compass) |
| Ollama | Local LLM runtime | [ollama.com](https://ollama.com/) |
| Gemini API key | Free, get one in 2 minutes | [aistudio.google.com](https://aistudio.google.com/) |

Create a project folder and a virtual environment before anything else:

```bash
mkdir wanderchat && cd wanderchat
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

---

## 2. Architecture overview (read this before coding)

```
[Bootstrap dataset: HuggingFace] ─┐
                                   ├──> [MongoDB: raw docs + vector index]
[Your own PRAW scraper, later] ───┘              │
                                                   │ (tool call)
[You: chat UI] ──> [LangGraph agent] ───> [Tool layer: vector search,
                         │                 live web search, weather,
                         │                 currency, itinerary builder]
                         │
                         └──> [LLM: Gemini Flash (dev) / Ollama (local)]
                                                   │
                              [Itinerary streamed back to you]
```

The agent isn't a single LLM call. It's a small loop: decide what's needed →
retrieve → check if the retrieval was actually good → retry or move on →
generate. This loop is what makes it "agentic" instead of a fixed pipeline.

---

## Phase 1 — MongoDB: data layer

**Learn first:** [MongoDB basics](https://www.mongodb.com/docs/manual/tutorial/getting-started/) (collections, documents, `find()`, `insert_many()`) if you've never touched it beyond basic retrieval.

### 1.1 Choose your MongoDB setup
Two options — pick one:

- **Option A (easiest): MongoDB Atlas free cloud cluster.** Sign up at
  [mongodb.com/atlas](https://www.mongodb.com/cloud/atlas/register), create a
  free M0 cluster, get a connection string. Vector search is built in.
  No Docker needed for this piece.
- **Option B (more Docker practice): run MongoDB locally in Docker** using
  the official `mongodb/mongodb-atlas-local` image, which — unlike plain
  community MongoDB — *does* support Atlas Search and Vector Search locally:
  ```bash
  docker run -p 27017:27017 --name atlas-local mongodb/mongodb-atlas-local
  ```
  This is the better choice if you specifically want more Docker reps, since
  it becomes one more service in your `docker-compose.yml` later.

Recommendation for a beginner: **start with Option A** to remove one variable
while you're learning the data/agent parts, then migrate to Option B once
everything works, as a deliberate "containerize it" exercise.

### 1.2 Get the bootstrap dataset
Download the WanderChat Reddit travel dataset from HuggingFace
(`soniawmeyer/reddit-travel-QA-finetuning`, MIT licensed, ~10.5k rows):

```bash
pip install datasets pandas
python -c "
from datasets import load_dataset
ds = load_dataset('soniawmeyer/reddit-travel-QA-finetuning')
ds['train'].to_pandas().to_csv('raw_reddit_travel.csv', index=False)
"
```

### 1.3 Clean and subset the data
You don't need all 10.5k rows. Filter down to a few thousand quality rows
using the dataset's own upvote-ratio / similarity columns, and combine
`title` + `selftext` into one field (the dataset's own docs note these
should be combined).

### 1.4 Design your MongoDB schema
```json
{
  "post_id": "abc123",
  "subreddit": "travel",
  "destination_tags": ["japan", "kyoto"],
  "question": "title + selftext combined",
  "tips": "comments field, the actual advice",
  "summary": "falcon_summary field",
  "upvote_ratio": 0.86,
  "embedding": [0.021, -0.114, "... 384 or 768 floats"]
}
```
`destination_tags` doesn't exist in the raw data — you'll need to extract it
yourself (simple keyword matching against a list of country/city names is
good enough for v1; don't over-engineer this).

### 1.5 Generate embeddings
Use a **local** embedding model so you're not burning Gemini quota on this
step and so it works even if you go fully offline later:

```bash
pip install sentence-transformers
```
```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")  # small, fast, good enough
embedding = model.encode("your combined question+tips text").tolist()
```

### 1.6 Load into MongoDB
Write a small script (`ingest.py`) that reads your cleaned CSV, generates an
embedding per row, and inserts into your `travel_tips` collection using
`pymongo`'s `insert_many()`.

### 1.7 Create the vector search index
In Atlas UI (or via `mongosh` for local): create a vector search index on
the `embedding` field, dimensions matching your model (384 for
`all-MiniLM-L6-v2`), similarity = cosine.

### 1.8 Test it manually before building anything else
Run a raw `$vectorSearch` aggregation query with a test embedding and
confirm you get sensible results back. **Don't move to Phase 2 until this
works** — everything downstream depends on it.

✅ **Phase 1 done when:** you can run a Python script that embeds a query
like "budget backpacking Japan" and gets back 5 relevant Reddit posts.

---

## Phase 2 — LangGraph: the agent

**Learn first:** [LangGraph quickstart](https://langchain-ai.github.io/langgraph/tutorials/introduction/) — do their basic chatbot tutorial before touching your own project. It's short and will save you hours of confusion later.

```bash
pip install langgraph langchain-google-genai python-dotenv
```

### 2.1 Define your agent's state
This is the shared data every node in your graph can read/write:
```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    retrieved_docs: list
    retry_count: int
```

### 2.2 Build your tools first, in isolation
Get each of these working as a plain Python function *before* wiring them
into the graph — much easier to debug one at a time:

- `search_stored_tips(query, destination=None)` → runs your Mongo vector
  search from Phase 1
- `live_search(query)` → wraps a web search API for anything not in your
  corpus
- `get_weather(location, dates)` → any free weather API
- `convert_currency(amount, from_currency, to_currency)` → any free FX API
- `build_itinerary(structured_plan)` → formats final output as structured
  text/JSON

### 2.3 Build the graph, node by node
Start with the simplest possible version — 3 nodes, no loop:
```
plan → retrieve → generate
```
Get this working end-to-end first. Then add the grading/retry loop:
```
plan → retrieve → grade_documents ─┬─(good)──> generate
                                    └─(bad)──> rewrite_question → retrieve (loop)
```
The `grade_documents` node is just another LLM call: "are these documents
relevant to this question? yes/no" — use structured output so it returns a
clean boolean, not prose you have to parse.

### 2.4 Wire in your LLM
Start with Gemini Flash — better tool-calling reliability while you're
learning, and 1,500 requests/day is far more than you'll use in development:
```python
from langchain_google_genai import ChatGoogleGenerativeAI
llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", api_key=GEMINI_API_KEY)
llm_with_tools = llm.bind_tools([search_stored_tips, live_search, get_weather, convert_currency])
```

### 2.5 Add memory (conversation history)
```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)

graph.invoke(
    {"messages": [{"role": "user", "content": "Plan 3 days in Lisbon"}]},
    config={"configurable": {"thread_id": "session-1"}}
)
```
Same `thread_id` = the agent remembers prior turns automatically. Swap
`MemorySaver` for a Mongo-backed checkpointer later so history survives
restarts (`langgraph-checkpoint-mongodb` package).

### 2.6 Test in a plain script or Jupyter notebook
Don't build the API or frontend yet. Get the agent working end-to-end from
the command line first — call it with a few different queries, confirm the
retrieval loop actually triggers on a bad query, confirm memory works across
two calls with the same `thread_id`.

✅ **Phase 2 done when:** you can have a multi-turn conversation with your
agent in a terminal script and it correctly uses retrieval + memory.

---

## Phase 3 — Local LLM toggle (Ollama)

**Learn first:** [Ollama's tool-calling docs](https://ollama.com/blog/tool-support).

### 3.1 Pull a model that supports tool-calling
```bash
ollama pull qwen2.5:8b
# or: ollama pull llama3.1:8b
```
Both run fine on an RTX 4060's 8GB VRAM.

### 3.2 Swap the LLM binding
```python
from langchain_ollama import ChatOllama
local_llm = ChatOllama(model="qwen2.5:8b").bind_tools([...])
```

### 3.3 Add a simple switch
An environment variable (`LLM_BACKEND=gemini` or `LLM_BACKEND=ollama`) that
picks which `llm_with_tools` object your graph uses. This is your "hybrid
cloud/local" story for the resume — keep it this simple, don't over-engineer
a plugin system for it.

### 3.4 Expect (and note in your README) that local tool-calling is less
reliable than Gemini's. This is exactly why the grading/rewrite loop from
Phase 2 matters — it catches bad retrieval regardless of which model
produced it.

✅ **Phase 3 done when:** the same agent code runs against both backends by
flipping one environment variable.

---

## Phase 4 — FastAPI backend

**Learn first:** [FastAPI's official tutorial](https://fastapi.tiangolo.com/tutorial/) (first few pages — you need routing and request bodies, not the whole thing).

```bash
pip install fastapi uvicorn
```

### 4.1 Basic structure
```
backend/
  main.py          # FastAPI app + routes
  agent.py         # your LangGraph graph from Phase 2
  db.py            # Mongo connection
  tools.py         # tool functions from Phase 2
```

### 4.2 One endpoint to start
```python
@app.post("/chat")
async def chat(message: str, thread_id: str):
    result = graph.invoke(
        {"messages": [{"role": "user", "content": message}]},
        config={"configurable": {"thread_id": thread_id}}
    )
    return {"response": result["messages"][-1].content}
```
Get this working with a plain non-streaming response first. Add streaming
(Server-Sent Events, via LangGraph's `.stream()` method) only after the
basic version works — streaming bugs are much harder to debug than
request/response bugs.

### 4.3 Long-term memory endpoint (optional, do this after everything else works)
A `/preferences` endpoint that reads/writes a small user-preferences document
in Mongo (budget style, preferred pace, dietary needs), which you load and
inject into the agent's context at the start of a new `thread_id`.

✅ **Phase 4 done when:** you can `curl` your `/chat` endpoint and get a real
itinerary response back.

---

## Phase 5 — Frontend

Pick one:

- **Streamlit** — fastest to build, less resume-polish, good for getting
  something working quickly. `pip install streamlit`.
- **React + Tailwind** — more setup, but looks more like a real product.
  Only do this if you're also comfortable with basic JS/React already —
  don't try to learn React *and* everything else in this plan at once.

Recommendation for a first pass: **Streamlit**. Swap to React later as a
follow-up improvement once the core system works — that's another good
"and then I improved it" story.

A minimal Streamlit chat loop:
```python
import streamlit as st
import requests

if "thread_id" not in st.session_state:
    st.session_state.thread_id = "session-" + str(uuid.uuid4())

prompt = st.chat_input("Where do you want to go?")
if prompt:
    response = requests.post("http://localhost:8000/chat",
                              json={"message": prompt, "thread_id": st.session_state.thread_id})
    st.write(response.json()["response"])
```

✅ **Phase 5 done when:** you can chat with your agent through a browser, not
just curl.

---

## Phase 6 — Docker

**Learn first:** [Docker Compose's official quickstart](https://docs.docker.com/compose/gettingstarted/) — build their example first, then come back.

### 6.1 Dockerfile for the backend
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 6.2 docker-compose.yml wiring it together
```yaml
services:
  mongo:
    image: mongodb/mongodb-atlas-local
    ports: ["27017:27017"]
    volumes: ["mongo_data:/data/db"]

  backend:
    build: ./backend
    ports: ["8000:8000"]
    environment:
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - MONGO_URI=mongodb://mongo:27017
      - LLM_BACKEND=gemini
    depends_on: [mongo]

  ollama:
    image: ollama/ollama
    ports: ["11434:11434"]
    volumes: ["ollama_data:/root/.ollama"]

  frontend:
    build: ./frontend
    ports: ["8501:8501"]
    depends_on: [backend]

volumes:
  mongo_data:
  ollama_data:
```
(Skip the `mongo` service entirely if you went with Atlas cloud in Phase 1 —
just point `MONGO_URI` at your Atlas connection string instead.)

### 6.3 Secrets
Put `GEMINI_API_KEY` in a `.env` file in the same folder (gitignored), Docker
Compose picks it up automatically.

### 6.4 Test it
```bash
docker compose up --build
```
Everything should come up and you should be able to chat through the
frontend exactly as before, now fully containerized.

✅ **Phase 6 done when:** `docker compose up` on a clean machine (or after
`docker compose down -v`) gets you a working chatbot with zero manual setup
steps.

---

## Phase 7 — Polish (do this last, don't skip it)

- **Small eval script:** a fixed list of ~10 test queries, check that
  retrieval returns relevant docs for each (even a manual "does this look
  right" check is better than nothing).
- **README.md:** architecture diagram (redraw the one from this plan),
  setup instructions, and a **credit to the WanderChat thesis project**
  for the bootstrap dataset — be explicit that you built on their public,
  MIT-licensed dataset rather than implying you scraped it yourself.
- **Clean commit history:** doesn't need to be perfect, but a `git log`
  that shows incremental progress (data layer → agent → API → frontend →
  Docker) tells a better story than one giant commit.
- **(Optional) LangSmith tracing:** free tier, lets you show a screenshot
  of the agent's actual reasoning/tool-call trace in your README — strong
  visual proof the "agentic" part is real.

---

## Phase 8 — Reddit API application (parallel track — don't block on this)

You already have this in motion. Keep it separate from the phases above:

1. Formal request already submitted via the Responsible Builder Policy form.
2. If/when approved: write a PRAW-based scraper (script provided earlier in
   this conversation) that supplements your bootstrap dataset with fresh
   posts, scheduled weekly via cron.
3. If never approved: no problem — the project is complete and correct
   without it. Mention in your README that this was evaluated and the
   bootstrap dataset was used instead, for the reasons discussed (API
   access policy change).

---

## Suggested build order (checklist)

- [ ] Phase 1 — MongoDB + bootstrap dataset + vector search working
- [ ] Phase 2 — LangGraph agent working in a terminal script
- [ ] Phase 4 — FastAPI wrapping the agent (skip Phase 3 for now)
- [ ] Phase 5 — Streamlit frontend talking to the API
- [ ] Phase 6 — Docker Compose containerizing everything
- [ ] Phase 3 — Add Ollama as a toggleable second backend
- [ ] Phase 7 — README, eval script, tracing, polish

Note the order: **get the whole thing working simply, end to end, before
adding the local-LLM toggle.** That's a deliberate choice — a complete
simple system beats a half-built complex one, every time you have to
explain it in an interview.

---

## Reference links (bookmark these)

- LangGraph docs: https://langchain-ai.github.io/langgraph/
- MongoDB Vector Search docs: https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-overview/
- Ollama tool-calling: https://ollama.com/blog/tool-support
- FastAPI tutorial: https://fastapi.tiangolo.com/tutorial/
- Docker Compose getting started: https://docs.docker.com/compose/gettingstarted/
- Gemini API docs: https://ai.google.dev/gemini-api/docs
- WanderChat dataset (credit this): https://huggingface.co/datasets/soniawmeyer/reddit-travel-QA-finetuning
