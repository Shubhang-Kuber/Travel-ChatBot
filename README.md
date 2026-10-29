## Person A — Data & Retrieval (MongoDB + RAG core)
 
**Responsibilities:**
- Dataset acquisition, cleaning, schema design
- Embedding pipeline, vector index setup
**Owns:**
- `search_stored_tips()` function and everything behind it
- MongoDB schema for both `travel_tips` and `users` collections
**Deliverable others depend on:**
- A working, tested `search_stored_tips(query, destination)` function with a stable signature
---
 
## Person B — Agent & LLM (LangGraph + tool orchestration)
 
**Responsibilities:**
- LangGraph state machine: plan / retrieve / grade / generate nodes
- Grade/retry loop logic
- Gemini + Ollama integration and the backend-switch env var
**Owns:**
- All other tool functions — `get_weather()`, `convert_currency()`, `live_search()`, `build_itinerary()`
**Depends on:**
- Person A's `search_stored_tips()` signature (agree on it in week 1, then build against a stub/mock until it's ready)
---
 
## Person C — Backend, Auth & Infra (FastAPI + Docker + deployment)
 
**Responsibilities:**
- FastAPI routes, request/response models, CORS
- Auth: signup/login, JWT issuance, bcrypt hashing, gateway-level token validation
- Rate limiting, load balancing config
- Docker Compose, Dockerfiles for each service, `.env` handling
**Owns:**
- The `/chat` and `/auth` endpoints that wrap Person B's graph
**Depends on:**
- Person B exposing the graph as an importable `graph.invoke()` call (agree on that interface early too)
---
 
## How We Avoid Conflict
 
- **Week 1:** all three agree together on the three interface contracts before anyone writes implementation code:
  1. `search_stored_tips()` signature
  2. `graph.invoke()` input/output shape
  3. `/chat` request/response JSON schema
- Each person works in their own module/folder (`data/`, `agent/`, `backend/`) — no two people editing the same file.
- Integration points are tested with mocks/stubs first (e.g. Person C can build the FastAPI route against a fake `graph.invoke()` returning dummy JSON, without waiting for Person B to finish).
- **Frontend** is picked up by whoever finishes their core piece first — it only needs the `/chat` endpoint contract, nothing else.