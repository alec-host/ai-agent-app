document.addEventListener('DOMContentLoaded', () => {
    const chatViewport = document.getElementById('chatViewport');
    const welcomeScreen = document.getElementById('welcomeScreen');
    const chatInput = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const mobileClose = document.getElementById('mobileClose');
    const starterChips = document.querySelectorAll('.chip, .nav-link[data-prompt]');
    const newChatBtn = document.getElementById('newChatBtn');

    // --- 1. SESSION IDENTITY LOGIC ---
    const sessionSettings = {
        tenantId: localStorage.getItem('tenantId') || '12345678',
        userRole: localStorage.getItem('userRole') || 'Associate',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
    };

    function updateIdentityBadge() {
        const badgeTenant = document.getElementById('badgeTenant');
        if (badgeTenant) {
            badgeTenant.textContent = sessionSettings.tenantId;
        }
    }

    function initSettingsModal() {
        const modal = document.getElementById('settingsModal');
        const openBtn = document.getElementById('openSettingsBtn');
        const saveBtn = document.getElementById('saveSettingsBtn');
        const tenantInput = document.getElementById('tenantInput');
        const roleSelect = document.getElementById('roleSelect');
        const timezoneInput = document.getElementById('timezoneInput');

        // Pre-fill
        tenantInput.value = sessionSettings.tenantId;
        roleSelect.value = sessionSettings.userRole;
        timezoneInput.value = sessionSettings.timezone;
        updateIdentityBadge();

        openBtn.addEventListener('click', () => {
            modal.classList.add('active');
        });

        saveBtn.addEventListener('click', () => {
            sessionSettings.tenantId = tenantInput.value;
            sessionSettings.userRole = roleSelect.value;

            localStorage.setItem('tenantId', sessionSettings.tenantId);
            localStorage.setItem('userRole', sessionSettings.userRole);

            updateIdentityBadge();
            modal.classList.remove('active');

            // Provide visual confirmation in the chat
            appendMessage('ai', `Identity updated. Session initialized for **Tenant ${sessionSettings.tenantId}** as **${sessionSettings.userRole}**.`);
        });

        // Close on backdrop click
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.classList.remove('active');
        });
    }

    initSettingsModal();

    let history = [];

    // --- 2. UI Interactions ---

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

    // Starter Chips & Sidebar Links
    starterChips.forEach(chip => {
        chip.addEventListener('click', (e) => {
            e.preventDefault();
            const prompt = chip.getAttribute('data-prompt');
            if (prompt) {
                sendMessage(prompt);
                if (window.innerWidth <= 768) {
                    sidebar.classList.remove('open');
                }
            }
        });
    });

    // Send on Enter (but not Shift+Enter)
    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const text = chatInput.value.trim();
            if (text) sendMessage(text);
        }
    });

    sendBtn.addEventListener('click', () => {
        const text = chatInput.value.trim();
        if (text) sendMessage(text);
    });

    // --- 3. Core Messaging Logic ---

    async function sendMessage(prompt) {
        if (!prompt.trim()) return;

        // Hide welcome screen if visible
        if (chatViewport.contains(welcomeScreen)) {
            chatViewport.innerHTML = '<div class="message-thread" id="messageThread"></div>';
        }

        const messageThread = document.getElementById('messageThread');

        appendMessage('user', prompt);
        chatInput.value = '';
        chatInput.style.height = 'auto';

        const loadingId = 'loading-' + Date.now();
        const indicator = document.createElement('div');
        indicator.className = 'message ai loading';
        indicator.id = loadingId;
        indicator.innerHTML = `
            <div class="message-icon"><i class="fas fa-robot"></i></div>
            <div class="message-content">
                <div class="typing-dots"><span></span><span></span><span></span></div>
            </div>
        `;
        messageThread.appendChild(indicator);
        chatViewport.scrollTo({ top: chatViewport.scrollHeight, behavior: 'smooth' });

        try {
            const response = await fetch('/ai/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Tenant-ID': sessionSettings.tenantId,
                    'X-User-Timezone': sessionSettings.timezone,
                    'User-Role': sessionSettings.userRole
                },
                body: JSON.stringify({ prompt, history })
            });

            const data = await response.json();
            const loadingEl = document.getElementById(loadingId);
            if (loadingEl) loadingEl.remove();

            if (data.response) {
                appendMessage('ai', data.response);
                history = data.history || [];
            } else if (data.status === 'auth_required') {
                const authMsg = `
                    <div class="auth-required-box" style="background: rgba(73, 124, 254, 0.1); padding: 20px; border-radius: 12px; border: 1px dashed var(--accent-color);">
                        <p><strong><i class="fab fa-google"></i> Google Calendar Access Required</strong></p>
                        <p style="margin: 8px 0; font-size: 0.875rem;">${data.message}</p>
                        <a href="${data.auth_url}" target="_blank" class="primary-btn" style="display:inline-block; width:auto; margin-top:10px; text-decoration:none; text-align:center;">Authorize Google Connection</a>
                    </div>
                `;
                appendMessage('ai', authMsg);
            } else {
                appendMessage('ai', 'Something went wrong. Please try again.');
            }
        } catch (error) {
            console.error('Fetch error:', error);
            const loadingEl = document.getElementById(loadingId);
            if (loadingEl) loadingEl.remove();
            appendMessage('ai', 'The AI service is currently unavailable. Please check your connection.');
        }
    }

    function appendMessage(role, content) {
        let messageThread = document.getElementById('messageThread');

        // Transition from welcome screen if needed
        if (!messageThread) {
            chatViewport.innerHTML = '<div class="message-thread" id="messageThread"></div>';
            messageThread = document.getElementById('messageThread');
        }

        const div = document.createElement('div');
        div.className = `message ${role}`;

        const icon = role === 'ai' ? '<i class="fas fa-robot"></i>' : '<i class="fas fa-user"></i>';

        // Use marked.js for AI responses
        const formattedContent = role === 'ai' ? marked.parse(content) : content;

        div.innerHTML = `
            <div class="message-icon">${icon}</div>
            <div class="message-content">${formattedContent}</div>
        `;

        messageThread.appendChild(div);
        chatViewport.scrollTo({ top: chatViewport.scrollHeight, behavior: 'smooth' });
    }
});
