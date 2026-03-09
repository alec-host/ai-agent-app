document.addEventListener('DOMContentLoaded', () => {
    const chatViewport = document.getElementById('chatViewport');
    const welcomeScreen = document.getElementById('welcomeScreen');
    const chatInput = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const mobileClose = document.getElementById('mobileClose');
    const starterChips = document.querySelectorAll('.chip');
    const newChatBtn = document.getElementById('newChatBtn');

    let history = [];
    const tenantId = "12345678"; // Demo Tenant ID

    // --- 1. UI Interactions ---

    // Auto-resize textarea
    chatInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = (this.scrollHeight) + 'px';
    });

    // Toggle Sidebar
    sidebarToggle.addEventListener('click', () => sidebar.classList.toggle('open'));
    mobileClose.addEventListener('click', () => sidebar.classList.remove('open'));

    // New Chat
    newChatBtn.addEventListener('click', () => {
        chatViewport.innerHTML = '';
        chatViewport.appendChild(welcomeScreen);
        history = [];
        chatInput.value = '';
        chatInput.style.height = 'auto';
    });

    // Starter Chips
    starterChips.forEach(chip => {
        chip.addEventListener('click', () => {
            const prompt = chip.getAttribute('data-prompt');
            chatInput.value = prompt;
            handleSendMessage();
        });
    });

    // Send on Enter (but not Shift+Enter)
    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendMessage();
        }
    });

    sendBtn.addEventListener('click', handleSendMessage);

    // --- 2. Core Messaging Logic ---

    async function handleSendMessage() {
        const text = chatInput.value.trim();
        if (!text) return;

        // Hide welcome screen if visible
        if (welcomeScreen.parentNode === chatViewport) {
            chatViewport.innerHTML = '<div class="message-thread" id="messageThread"></div>';
        }

        const messageThread = document.getElementById('messageThread');

        // Append User Message
        appendMessage('user', text);
        chatInput.value = '';
        chatInput.style.height = 'auto';

        // Add Loading Indicator
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'message ai loading';
        loadingDiv.innerHTML = `
            <div class="message-icon"><i class="fas fa-robot"></i></div>
            <div class="message-content">Thinking...</div>
        `;
        messageThread.appendChild(loadingDiv);
        scrollTobottom();

        try {
            // CALL THE REAL AI AGENT API
            const response = await fetch('/ai/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Tenant-ID': tenantId
                },
                body: JSON.stringify({
                    prompt: text,
                    history: history
                })
            });

            const data = await response.json();

            // Remove loading indicator
            messageThread.removeChild(loadingDiv);

            if (data.response) {
                appendMessage('ai', data.response);
                history = data.history || [];
            } else if (data.status === "auth_required") {
                const authMsg = `${data.message} <br><br> <a href="${data.auth_url}" target="_blank" class="auth-link">Click here to authorize Google Calendar</a>`;
                appendMessage('ai', authMsg);
            }
        } catch (error) {
            console.error("Fetch Error:", error);
            messageThread.removeChild(loadingDiv);
            appendMessage('ai', "I'm having trouble connecting to the firm's operations server. Please try again in a moment.");
        }
    }

    function appendMessage(role, content) {
        const messageThread = document.getElementById('messageThread');
        const div = document.createElement('div');
        div.className = `message ${role}`;

        const icon = role === 'ai' ? '<i class="fas fa-robot"></i>' : '<i class="fas fa-user"></i>';

        // Use marked.js for AI responses to handle tables and markdown
        const formattedContent = role === 'ai' ? marked.parse(content) : content;

        div.innerHTML = `
            <div class="message-icon">${icon}</div>
            <div class="message-content">${formattedContent}</div>
        `;

        messageThread.appendChild(div);
        scrollTobottom();
    }

    function scrollTobottom() {
        chatViewport.scrollTop = chatViewport.scrollHeight;
    }
});
