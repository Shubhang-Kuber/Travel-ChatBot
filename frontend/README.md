# TravelBot frontend

React 18 + TypeScript + Vite. Talks to the FastAPI backend through a same-origin `/api` prefix
(contract: `../backend/CONTRACT.md`).

## Develop

```powershell
cd frontend
npm install
npm run dev        # http://localhost:5173, /api proxied to http://localhost:8000
```

Point the dev proxy at another backend with `BACKEND_URL` (for example `$env:BACKEND_URL="http://localhost:9000"`).

## Build and run in Docker

```powershell
docker compose up --build frontend    # http://localhost:8501 (nginx serves dist/ and proxies /api to the backend)
```

## Layout

| Path | What it holds |
|---|---|
| `src/api/client.ts` | Token storage, single-flight refresh, SSE streaming (`/chat/stream`), error mapping |
| `src/hooks/useChat.ts` | Chat state, typing reveal, progress steps |
| `src/lib/answer.ts` | Parses the agent's `Itinerary for … / Day N: … / Notes` text into blocks |
| `src/lib/itinerary.ts` | Trip stops, validation, and the message sent to the agent |
| `src/components/` | `AuthScreen`, `Planner`, `Chat`, `Message`, `Brand` |
| `src/styles/index.css` | Design tokens (light and dark) and all styles |
