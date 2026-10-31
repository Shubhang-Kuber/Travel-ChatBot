// Backend is reached through the same origin: nginx proxies /api/* to FastAPI (see nginx.conf.template).
const API_BASE = '/api';
// Shown in turn while the agent works, so the wait reads as progress.
const PROGRESS_STEPS = ['Understanding your request', 'Searching travel tips', 'Checking the details', 'Writing your answer'];
const PROGRESS_STEP_MS = 3500;

const STORAGE = {
    access: 'travelbot.access_token',
    refresh: 'travelbot.refresh_token',
    email: 'travelbot.email',
    thread: 'travelbot.thread_id',
};

let authMode = 'login';
let busy = false;

// ---------------------------------------------------------------- storage

function load(key) {
    try { return localStorage.getItem(key); } catch { return null; }
}

function store(key, value) {
    try {
        if (value == null) localStorage.removeItem(key);
        else localStorage.setItem(key, value);
    } catch { /* private mode: session lasts until reload */ }
}

function saveTokens(tokens) {
    // Refresh token first: it is single use, so it must be kept before the access token is used.
    store(STORAGE.refresh, tokens.refresh_token);
    store(STORAGE.access, tokens.access_token);
}

function isLoggedIn() {
    return Boolean(load(STORAGE.refresh));
}

// ---------------------------------------------------------------- API

class ApiError extends Error {
    constructor(status, code, message, retryAfter = null) {
        super(message);
        this.status = status;
        this.code = code;
        this.retryAfter = retryAfter;
    }
}

async function toApiError(res) {
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    const err = (body && body.error) || {};
    return new ApiError(res.status, err.code || `http_${res.status}`,
        err.message || res.statusText || 'Request failed.', res.headers.get('Retry-After'));
}

function postJson(path, body, accessToken = null) {
    const headers = { 'Content-Type': 'application/json' };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    return fetch(`${API_BASE}${path}`, { method: 'POST', headers, body: JSON.stringify(body) });
}

// Single-flight: two parallel refreshes with the same token count as a replay and log the user out.
let refreshing = null;
function refreshTokens() {
    if (!refreshing) {
        refreshing = (async () => {
            const token = load(STORAGE.refresh);
            if (!token) throw new ApiError(401, 'invalid_token', 'Session expired.');
            const res = await postJson('/auth/refresh', { refresh_token: token });
            if (!res.ok) throw await toApiError(res);
            saveTokens(await res.json());
        })().finally(() => { refreshing = null; });
    }
    return refreshing;
}

async function authedPost(path, body) {
    let res = await postJson(path, body, load(STORAGE.access));
    if (res.status === 401) {
        try {
            await refreshTokens();
        } catch (err) {
            signOut('Your session expired. Please sign in again.');
            throw err;
        }
        res = await postJson(path, body, load(STORAGE.access));
    }
    if (!res.ok) {
        const err = await toApiError(res);
        if (res.status === 401) signOut('Your session expired. Please sign in again.');
        throw err;
    }
    return res;
}

// POST /chat/stream: SSE events token / done / error (backend/CONTRACT.md).
async function streamChat(message, onToken) {
    const body = { message };
    const threadId = load(STORAGE.thread);
    if (threadId) body.thread_id = threadId;

    const res = await authedPost('/chat/stream', body);
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
        const { value, done } = await reader.read();
        if (done) throw new ApiError(0, 'stream_interrupted', 'The connection dropped before the answer finished.');
        buffer += value;
        let sep;
        while ((sep = buffer.indexOf('\n\n')) !== -1) {
            const block = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const event = block.match(/^event: (.*)$/m)?.[1];
            const dataLine = block.match(/^data: (.*)$/m)?.[1];
            const data = dataLine ? JSON.parse(dataLine) : null;
            if (event === 'token') {
                onToken(data.content);
            } else if (event === 'done') {
                store(STORAGE.thread, data.thread_id);  // continue this conversation next turn
                return;
            } else if (event === 'error') {
                throw new ApiError(200, data.error.code, data.error.message);
            }
        }
    }
}

function describeError(err) {
    if (err instanceof ApiError) {
        if (err.code === 'rate_limited') {
            return `Too many requests. Try again in ${err.retryAfter || 'a few'} seconds.`;
        }
        if (err.code === 'validation_error') return 'Please check the details you entered.';
        return err.message;
    }
    return 'Could not reach the TravelBot server. Is the backend running?';
}

// ---------------------------------------------------------------- terminal messages

function clearCursors() {
    document.querySelectorAll('#terminal-body .cursor').forEach(cursor => cursor.remove());
}

// AI messages type out with a cursor; text can keep arriving (streaming) while it types.
function createMessage(sender = 'ai', placeholder = null) {
    const terminalBody = document.getElementById('terminal-body');
    clearCursors();

    const messageDiv = document.createElement('div');
    messageDiv.classList.add('message', sender);

    const textSpan = document.createElement('span');
    messageDiv.appendChild(textSpan);

    let placeholderSpan = null;
    if (placeholder) {
        placeholderSpan = document.createElement('span');
        placeholderSpan.classList.add('thinking');
        placeholderSpan.textContent = placeholder;
        messageDiv.appendChild(placeholderSpan);
    }

    const cursor = document.createElement('span');
    cursor.classList.add('cursor');
    messageDiv.appendChild(cursor);

    terminalBody.appendChild(messageDiv);
    terminalBody.scrollTop = terminalBody.scrollHeight;

    let queue = '';
    let timer = null;
    let idleResolvers = [];

    function typeText() {
        if (!queue) {
            timer = null;
            idleResolvers.forEach(resolve => resolve());
            idleResolvers = [];
            return;
        }
        // One step per frame, proportional to the backlog: a full answer drains in well under a second.
        const n = Math.max(3, Math.ceil(queue.length / 6));
        textSpan.textContent += queue.slice(0, n);
        queue = queue.slice(n);
        terminalBody.scrollTop = terminalBody.scrollHeight;
        timer = requestAnimationFrame(typeText);
    }

    return {
        element: messageDiv,
        write(text) {
            if (!text) return;
            if (placeholderSpan) {
                placeholderSpan.remove();
                placeholderSpan = null;
            }
            queue += text;
            if (!timer) typeText();
        },
        idle() {
            return new Promise(resolve => {
                if (!timer && !queue) resolve();
                else idleResolvers.push(resolve);
            });
        },
        fail(text) {
            if (placeholderSpan) placeholderSpan.remove();
            messageDiv.classList.add('error');
            this.write(text);
        },
    };
}

function addMessage(text, sender = 'ai') {
    if (sender === 'user') {
        clearCursors();
        const terminalBody = document.getElementById('terminal-body');
        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message', 'user');
        messageDiv.textContent = text;
        terminalBody.appendChild(messageDiv);
        terminalBody.scrollTop = terminalBody.scrollHeight;
        return null;
    }
    const message = createMessage(sender);
    message.write(text);
    return message;
}

function clearTerminal() {
    document.getElementById('terminal-body').replaceChildren();
}

// ---------------------------------------------------------------- chat

function setBusy(value) {
    busy = value;
    const loggedIn = isLoggedIn();
    document.getElementById('chat-input').disabled = value || !loggedIn;
    document.getElementById('chat-send').disabled = value || !loggedIn;
    document.getElementById('plan-submit').disabled = value;
}

async function sendMessage(text, displayText = text) {
    if (busy || !text.trim()) return;
    setBusy(true);
    addMessage(displayText, 'user');
    const reply = createMessage('ai', PROGRESS_STEPS[0]);
    let received = false;
    let step = 0;
    const progress = setInterval(() => {
        const label = reply.element.querySelector('.thinking');
        if (!label || step >= PROGRESS_STEPS.length - 1) return;
        label.textContent = PROGRESS_STEPS[++step];
    }, PROGRESS_STEP_MS);
    try {
        await streamChat(text, token => {
            received = true;
            reply.write(token);
        });
        if (!received) reply.write('(no answer was returned)');
    } catch (err) {
        reply.fail(`Error: ${describeError(err)}`);
    } finally {
        clearInterval(progress);
        await reply.idle();
        setBusy(false);
        if (isLoggedIn()) document.getElementById('chat-input').focus();
    }
}

document.getElementById('chat-form').addEventListener('submit', event => {
    event.preventDefault();
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendMessage(text);
});

// ---------------------------------------------------------------- auth

function setAuthMode(mode) {
    authMode = mode;
    document.querySelectorAll('.auth-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.mode === mode);
    });
    const password = document.getElementById('auth-password');
    password.autocomplete = mode === 'signup' ? 'new-password' : 'current-password';
    if (mode === 'signup') password.minLength = 8;
    else password.removeAttribute('minlength');
    document.getElementById('auth-submit-label').textContent = mode === 'signup' ? 'Create account' : 'Sign in';
    showAuthError(null);
}

function showAuthError(text) {
    const el = document.getElementById('auth-error');
    el.textContent = text || '';
    el.hidden = !text;
}

async function login(email, password) {
    const res = await postJson('/auth/login', { email, password });
    if (!res.ok) throw await toApiError(res);
    saveTokens(await res.json());
    store(STORAGE.email, email.trim().toLowerCase());
}

document.querySelectorAll('.auth-tab').forEach(tab => {
    tab.addEventListener('click', () => setAuthMode(tab.dataset.mode));
});

document.getElementById('auth-form').addEventListener('submit', async event => {
    event.preventDefault();
    const email = document.getElementById('auth-email').value;
    const password = document.getElementById('auth-password').value;
    const submit = document.getElementById('auth-submit');
    submit.disabled = true;
    showAuthError(null);
    try {
        if (authMode === 'signup') {
            const res = await postJson('/auth/signup', { email, password });
            if (!res.ok) throw await toApiError(res);
        }
        await login(email, password);
        document.getElementById('auth-password').value = '';
        renderSession(true);
    } catch (err) {
        if (err.code === 'email_taken') showAuthError('That email is already registered. Sign in instead.');
        else if (err.code === 'invalid_credentials') showAuthError('Wrong email or password.');
        else if (err.code === 'validation_error' && authMode === 'signup') {
            showAuthError('Use a valid email and a password of 8 to 72 characters.');
        } else showAuthError(describeError(err));
    } finally {
        submit.disabled = false;
    }
});

function signOut(notice = null) {
    const refresh = load(STORAGE.refresh);
    if (refresh) postJson('/auth/logout', { refresh_token: refresh }).catch(() => {});
    Object.values(STORAGE).forEach(key => store(key, null));
    renderSession(false, notice);
}

document.getElementById('sign-out').addEventListener('click', () => signOut());

document.getElementById('new-trip').addEventListener('click', () => {
    if (busy) return;
    store(STORAGE.thread, null);
    clearTerminal();
    addMessage('New trip started.\nPlease enter your travel itinerary ✈️ or ask me anything below.', 'ai');
    expandForm();
});

function renderSession(loggedIn, notice = null) {
    document.getElementById('auth-content').hidden = loggedIn;
    document.getElementById('plan-content').hidden = !loggedIn;
    document.getElementById('terminal-actions').hidden = !loggedIn;
    const email = load(STORAGE.email) || '';
    document.getElementById('terminal-user').textContent = email;
    document.getElementById('chat-input').placeholder = loggedIn
        ? 'Ask anything, e.g. what about food?'
        : 'Sign in to chat';
    setBusy(false);

    clearTerminal();
    if (loggedIn) {
        const name = email.split('@')[0] || 'USER';
        const resumed = load(STORAGE.thread) ? '\n(Continuing your last trip conversation. Click "New trip" to start over.)' : '';
        addMessage(`Welcome, ${name}\nPlease enter your travel itinerary ✈️ or ask me anything below.${resumed}`, 'ai');
    } else {
        expandForm();
        addMessage(notice || 'Welcome, traveller\nSign in or create an account to start planning ✈️', 'ai');
    }
}

// ---------------------------------------------------------------- itinerary form

// Add stop button event
document.getElementById('add-stop').addEventListener('click', () => {
    addStopRow();
});

// Minimize/Expand form functionality
document.getElementById('minimize-btn').addEventListener('click', () => {
    minimizeForm();
});

document.getElementById('expand-icon').addEventListener('click', () => {
    expandForm();
});


function minimizeForm() {
    document.getElementById('app').classList.add('chat-mode');
    document.getElementById('form-section').classList.add('minimized');
}

function expandForm() {
    document.getElementById('app').classList.remove('chat-mode');
    document.getElementById('form-section').classList.remove('minimized');
}

function tripDays(from, to) {
    return Math.round((new Date(to) - new Date(from)) / 86400000) + 1;
}

function itineraryMessage(stops, notes) {
    const lines = stops.map((stop, i) => {
        const days = tripDays(stop.from, stop.to);
        return `${i + 1}. ${stop.destination}: ${stop.from} to ${stop.to} (${days} day${days === 1 ? '' : 's'})`;
    });
    let message = `Plan an itinerary for my trip:\n${lines.join('\n')}`;
    if (notes) message += `\nNotes: ${notes}`;
    return message;
}

// Form submit event
document.getElementById('itinerary-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;

    const stops = [];
    const stopEntries = document.querySelectorAll('.stop-entry');

    stopEntries.forEach(entry => {
        const destination = entry.querySelector('[name="destination"]').value.trim();
        const fromDate = entry.querySelector('[name="from"]').value;
        const toDate = entry.querySelector('[name="to"]').value;

        if (destination && fromDate && toDate) {
            stops.push({
                destination: destination,
                from: fromDate,
                to: toDate
            });
        }
    });

    if (stops.length === 0) {
        addMessage('Please add at least one destination to your itinerary.', 'ai');
        return;
    }
    const backwards = stops.find(stop => stop.to < stop.from);
    if (backwards) {
        addMessage(`The dates for ${backwards.destination} end before they start. Please fix them.`, 'ai');
        return;
    }

    const message = itineraryMessage(stops, document.getElementById('trip-notes').value.trim());

    // Minimize form after submission
    setTimeout(() => {
        minimizeForm();
    }, 1000);

    await sendMessage(message);
});

function addStopRow() {
    const container = document.getElementById('stops-container');
    const stopNumber = container.children.length + 1;

    const stopDiv = document.createElement('div');
    stopDiv.classList.add('stop-entry');
    stopDiv.innerHTML = `
        <div class="stop-header">
            <div class="stop-number">${stopNumber}</div>
            <button type="button" class="remove-btn" onclick="removeStop(this)">
                <i class="fas fa-times"></i> Remove
            </button>
        </div>
        <div class="form-grid">
            <div class="form-group">
                <label class="form-label">Destination</label>
                <input type="text" name="destination" class="form-input" placeholder="e.g. Paris, France" required>
            </div>
            <div class="form-group">
                <label class="form-label">From Date</label>
                <input type="date" name="from" class="form-input" required>
            </div>
            <div class="form-group">
                <label class="form-label">To Date</label>
                <input type="date" name="to" class="form-input" required>
            </div>
        </div>
    `;

    container.appendChild(stopDiv);

    // Set minimum date to today for new inputs
    const today = new Date().toISOString().split('T')[0];
    stopDiv.querySelectorAll('input[type="date"]').forEach(input => {
        input.min = today;
    });

    // Animate entry
    setTimeout(() => {
        stopDiv.style.opacity = '0';
        stopDiv.style.transform = 'translateY(20px)';
        stopDiv.style.transition = 'all 0.3s ease';

        requestAnimationFrame(() => {
            stopDiv.style.opacity = '1';
            stopDiv.style.transform = 'translateY(0)';
        });
    }, 10);
}

function removeStop(button) {
    const stopEntry = button.closest('.stop-entry');
    stopEntry.style.transition = 'all 0.3s ease';
    stopEntry.style.opacity = '0';
    stopEntry.style.transform = 'translateY(-20px)';

    setTimeout(() => {
        stopEntry.remove();
        updateStopNumbers();
    }, 300);
}

function updateStopNumbers() {
    const stopEntries = document.querySelectorAll('.stop-entry');
    stopEntries.forEach((entry, index) => {
        const numberDiv = entry.querySelector('.stop-number');
        numberDiv.textContent = index + 1;
    });
}

// ---------------------------------------------------------------- start

// Initialize with first stop
addStopRow();
setAuthMode('login');
renderSession(isLoggedIn());
