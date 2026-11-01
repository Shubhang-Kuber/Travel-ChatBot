// Initialize with welcome message and cursor
addMessage('Welcome, USER\nPlease enter your travel itinerary ✈️', 'ai');

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
    
    const stops = [];
    const stopEntries = document.querySelectorAll('.stop-entry');
    
    stopEntries.forEach(entry => {
        const destination = entry.querySelector('[name="destination"]').value;
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

    if (stops.length > 0) {
        addMessage(`Processing itinerary with ${stops.length} stops...`, 'ai');
        
        // Minimize form after submission
        setTimeout(() => {
            minimizeForm();
        }, 1000);
        
        // Simulate API call (replace with your actual endpoint)
        try {
            const response = await fetch('/send-itinerary', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ itinerary: stops })
            });

            const resText = await response.text();
            if (resText) {
                addMessage(resText, 'ai');
            }
        } catch (error) {
            addMessage(`Error: ${error.message}`, 'ai');
        }
    } else {
        addMessage('Please add at least one destination to your itinerary.', 'ai');
    }
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

function addMessage(text, sender = 'ai') {
    const terminalBody = document.getElementById('terminal-body');
    
    // Remove cursor from previous message
    const existingCursors = terminalBody.querySelectorAll('.cursor');
    existingCursors.forEach(cursor => cursor.remove());
    
    const messageDiv = document.createElement('div');
    messageDiv.classList.add('message', sender);
    
    const textSpan = document.createElement('span');
    messageDiv.appendChild(textSpan);
    
    // Add cursor
    const cursor = document.createElement('span');
    cursor.classList.add('cursor');
    messageDiv.appendChild(cursor);
    
    terminalBody.appendChild(messageDiv);
    
    // Typing effect
    let i = 0;
    function typeText() {
        if (i < text.length) {
            textSpan.textContent += text.charAt(i);
            i++;
            terminalBody.scrollTop = terminalBody.scrollHeight;
            setTimeout(typeText, 30);
        } else {
            // Keep cursor blinking at the end
            terminalBody.scrollTop = terminalBody.scrollHeight;
        }
    }
    
    typeText();
}

// Set minimum date to today for initial load
document.addEventListener('DOMContentLoaded', function() {
    const today = new Date().toISOString().split('T')[0];
    document.querySelectorAll('input[type="date"]').forEach(input => {
        input.min = today;
    });
});