// Backend is reached through the same origin: nginx (prod) or the Vite dev server proxies /api/* to FastAPI.
// Contract: backend/CONTRACT.md.
const API_BASE = '/api';

const STORAGE = {
  access: 'travelbot.access_token',
  refresh: 'travelbot.refresh_token',
  email: 'travelbot.email',
  thread: 'travelbot.thread_id',
} as const;

// ---------------------------------------------------------------- storage

function load(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string | null) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private mode: session lasts until reload */
  }
}

export const session = {
  isLoggedIn: () => Boolean(load(STORAGE.refresh)),
  email: () => load(STORAGE.email) ?? '',
  threadId: () => load(STORAGE.thread),
  setThreadId: (id: string | null) => store(STORAGE.thread, id),
  clear: () => Object.values(STORAGE).forEach(key => store(key, null)),
};

interface Tokens {
  access_token: string;
  refresh_token: string;
}

function saveTokens(tokens: Tokens) {
  // Refresh token first: it is single use, so it must be kept before the access token is used.
  store(STORAGE.refresh, tokens.refresh_token);
  store(STORAGE.access, tokens.access_token);
}

// Called when the session is gone for good (refresh rejected), so the UI can show the sign-in screen.
let onSessionExpired: () => void = () => {};
export function setSessionExpiredHandler(handler: () => void) {
  onSessionExpired = handler;
}

// ---------------------------------------------------------------- errors

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public retryAfter: string | null = null,
  ) {
    super(message);
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  let body: { error?: { code?: string; message?: string } } | null = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const err = body?.error ?? {};
  return new ApiError(
    res.status,
    err.code ?? `http_${res.status}`,
    err.message ?? (res.statusText || 'Request failed.'),
    res.headers.get('Retry-After'),
  );
}

export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'rate_limited') {
      return `Too many requests. Try again in ${err.retryAfter ?? 'a few'} seconds.`;
    }
    if (err.code === 'validation_error') return 'Check the details you entered and try again.';
    return err.message;
  }
  return 'Could not reach the TravelBot server. Check that the backend is running.';
}

// ---------------------------------------------------------------- requests

function postJson(path: string, body: unknown, accessToken: string | null = null, signal?: AbortSignal) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  return fetch(`${API_BASE}${path}`, { method: 'POST', headers, body: JSON.stringify(body), signal });
}

// Single-flight: two parallel refreshes with the same token count as a replay and log the user out everywhere.
let refreshing: Promise<void> | null = null;
function refreshTokens(): Promise<void> {
  if (!refreshing) {
    refreshing = (async () => {
      const token = load(STORAGE.refresh);
      if (!token) throw new ApiError(401, 'invalid_token', 'Session expired.');
      const res = await postJson('/auth/refresh', { refresh_token: token });
      if (!res.ok) throw await toApiError(res);
      saveTokens(await res.json());
    })().finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

async function authedPost(path: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  let res = await postJson(path, body, load(STORAGE.access), signal);
  if (res.status === 401) {
    try {
      await refreshTokens();
    } catch (err) {
      signOut();
      onSessionExpired();
      throw err;
    }
    res = await postJson(path, body, load(STORAGE.access), signal);
  }
  if (!res.ok) {
    const err = await toApiError(res);
    if (res.status === 401) {
      signOut();
      onSessionExpired();
    }
    throw err;
  }
  return res;
}

// ---------------------------------------------------------------- auth

export async function signUp(email: string, password: string) {
  const res = await postJson('/auth/signup', { email, password });
  if (!res.ok) throw await toApiError(res);
}

export async function logIn(email: string, password: string) {
  const res = await postJson('/auth/login', { email, password });
  if (!res.ok) throw await toApiError(res);
  saveTokens(await res.json());
  store(STORAGE.email, email.trim().toLowerCase());
}

export function signOut() {
  const refresh = load(STORAGE.refresh);
  if (refresh) postJson('/auth/logout', { refresh_token: refresh }).catch(() => {});
  session.clear();
}

// ---------------------------------------------------------------- chat

// POST /chat/stream: SSE events token / done / error.
export async function streamChat(message: string, onToken: (text: string) => void, signal?: AbortSignal) {
  const body: { message: string; thread_id?: string } = { message };
  const threadId = session.threadId();
  if (threadId) body.thread_id = threadId;

  const res = await authedPost('/chat/stream', body, signal);
  if (!res.body) throw new ApiError(0, 'stream_interrupted', 'The server sent an empty response.');
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw new ApiError(0, 'stream_interrupted', 'The connection dropped before the answer finished.');
    buffer += value;
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const event = block.match(/^event: (.*)$/m)?.[1];
      const dataLine = block.match(/^data: (.*)$/m)?.[1];
      const data = dataLine ? JSON.parse(dataLine) : null;
      if (event === 'token') {
        onToken(data.content);
      } else if (event === 'done') {
        session.setThreadId(data.thread_id); // continue this conversation next turn
        return;
      } else if (event === 'error') {
        throw new ApiError(200, data.error.code, data.error.message);
      }
    }
  }
}
