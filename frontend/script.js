// Backend is reached through the same origin: nginx proxies /api/* to FastAPI (see nginx.conf.template).
const API_BASE = '/api';
const THREAD_KEY = 'travelbot.thread_id';
// Shown in turn while the agent works, so the wait reads as progress.
const PROGRESS_STEPS = ['Understanding your request', 'Searching travel tips', 'Checking the details', 'Writing your answer'];
const PROGRESS_STEP_MS = 3500;

let busy = false;

// ---------------------------------------------------------------- storage

function loadThread() {
    try { return localStorage.getItem(THREAD_KEY); } catch { return null; }
}

function saveThread(value) {
    try {
        if (value == null) localStorage.removeItem(THREAD_KEY);
        else localStorage.setItem(THREAD_KEY, value);
    } catch { /* private mode: lasts until reload */ }
}

// ---------------------------------------------------------------- API

class ApiError extends Error {
    constructor(code, message, retryAfter = null) {
        super(message);
        this.code = code;
        this.retryAfter = retryAfter;
    }
}

async function toApiError(res) {
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    const err = (body && body.error) || {};
    return new ApiError(err.code || `http_${res.status}`, err.message || res.statusText || 'Request failed.',
        res.headers.get('Retry-After'));
}

// POST /chat/stream: SSE events token / done / error (backend/CONTRACT.md).
async function streamChat(message, onToken) {
    const body = { message };
    const threadId = loadThread();
    if (threadId) body.thread_id = threadId;

    const res = await fetch(`${API_BASE}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) throw await toApiError(res);

    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
        const { value, done } = await reader.read();
        if (done) throw new ApiError('stream_interrupted', 'The connection dropped before the answer finished.');
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
                saveThread(data.thread_id);  // continue this conversation next time
                return;
            } else if (event === 'error') {
                throw new ApiError(data.error.code, data.error.message);
            }
        }
    }
}

function describeError(err) {
    if (err instanceof ApiError) {
        if (err.code === 'rate_limited') return `Too many requests. Try again in ${err.retryAfter || 'a few'} seconds.`;
        if (err.code === 'validation_error') return 'Please check your message and try again.';
        return err.message;
    }
    return 'Could not reach the TravelBot server. Is the backend running?';
}

// ---------------------------------------------------------------- terminal messages

// AI messages type out with a cursor. Text can keep arriving (streaming) while it types.
function clearCursors() {
    document.querySelectorAll('#terminal-body .cursor').forEach(cursor => cursor.remove());
}

function createMessage(sender = 'ai', placeholder = null) {
    const terminalBody = document.getElementById('terminal-body');

    // Remove cursor from previous message
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
            terminalBody.scrollTop = terminalBody.scrollHeight;
            return;
        }
        // Speed up with the backlog so long streamed answers don't lag far behind.
        const n = Math.max(1, Math.ceil(queue.length / 20));
        textSpan.textContent += queue.slice(0, n);
        queue = queue.slice(n);
        terminalBody.scrollTop = terminalBody.scrollHeight;
        timer = setTimeout(typeText, 30);
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
        // User messages appear at once, without typing.
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

// ---------------------------------------------------------------- itinerary -> chat

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

function setBusy(value) {
    busy = value;
    document.getElementById('chat-input').disabled = value;
    document.getElementById('chat-send').disabled = value;
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
        document.getElementById('chat-input').focus();
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

document.getElementById('new-trip').addEventListener('click', () => {
    if (busy) return;
    saveThread(null);
    clearTerminal();
    addMessage('New trip started.\nPlease enter your travel itinerary ✈️ or ask me anything below.', 'ai');
    expandForm();
});

// Initialize with welcome message and cursor
const resumed = loadThread() ? '\n(Continuing your last trip conversation. Click "New trip" to start over.)' : '';
addMessage(`Welcome, traveller\nPlease enter your travel itinerary ✈️ or ask me anything below.${resumed}`, 'ai');

// Initialize with first stop
addStopRow();

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
    const container = document.querySelector('.container');
    const formSection = document.getElementById('form-section');

    container.classList.add('chat-mode');
    formSection.classList.add('minimized');
}

function expandForm() {
    const container = document.querySelector('.container');
    const formSection = document.getElementById('form-section');

    container.classList.remove('chat-mode');
    formSection.classList.remove('minimized');
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
