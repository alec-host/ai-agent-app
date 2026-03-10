const SESSION_EXPIRY_MS = 60 * 60 * 1000; // 1 Hour

const safeStorage = {
    get: (key, fallback) => {
        try {
            const lastActivity = localStorage.getItem('lastActivity');
            const tenantId = localStorage.getItem('tenantId');

            // If expired, consider it empty
            if (lastActivity && (Date.now() - parseInt(lastActivity)) > SESSION_EXPIRY_MS) {
                return fallback;
            }
            return localStorage.getItem(key) || fallback;
        } catch (e) {
            return fallback;
        }
    },
    set: (key, val) => {
        try {
            localStorage.setItem(key, val);
            localStorage.setItem('lastActivity', Date.now());
        } catch (e) {
            console.error(`Failed to save ${key}`);
        }
    },
    updateActivity: () => {
        try {
            if (localStorage.getItem('tenantId')) {
                localStorage.setItem('lastActivity', Date.now());
            }
        } catch (e) { }
    }
};

document.addEventListener('DOMContentLoaded', () => {
    // DOM CACHE
    const nodes = {
        chatViewport: document.getElementById('chatViewport'),
        welcomeScreen: document.getElementById('welcomeScreen'),
        chatInput: document.getElementById('chatInput'),
        sendBtn: document.getElementById('sendBtn'),
        sidebar: document.getElementById('sidebar'),
        sidebarToggle: document.getElementById('sidebarToggle'),
        mobileClose: document.getElementById('mobileClose'),
        starterChips: document.querySelectorAll('.chip, .nav-link[data-prompt]'),
        newChatBtn: document.getElementById('newChatBtn'),
        badgeTenant: document.getElementById('badgeTenant'),
        modal: document.getElementById('settingsModal'),
        tenantInput: document.getElementById('tenantInput'),
        roleSelect: document.getElementById('roleSelect'),
        timezoneInput: document.getElementById('timezoneInput'),
        saveBtn: document.getElementById('saveSettingsBtn'),
        themeToggle: document.getElementById('themeToggle')
    };

    // --- SESSION IDENTITY STATE ---
    const sessionSettings = {
        tenantId: safeStorage.get('tenantId', null),
        userRole: safeStorage.get('userRole', 'Associate'),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
    };

    let history = [];

    function updateUIIdentity() {
        if (nodes.badgeTenant) nodes.badgeTenant.textContent = sessionSettings.tenantId;
        if (nodes.tenantInput) nodes.tenantInput.value = sessionSettings.tenantId;
        if (nodes.roleSelect) nodes.roleSelect.value = sessionSettings.userRole;
        if (nodes.timezoneInput) nodes.timezoneInput.value = sessionSettings.timezone;
    }

    function isSessionValid() {
        const tenantId = localStorage.getItem('tenantId');
        const lastActivity = localStorage.getItem('lastActivity');
        if (!tenantId || !lastActivity) return false;

        const expired = (Date.now() - parseInt(lastActivity)) > SESSION_EXPIRY_MS;
        if (expired) {
            localStorage.removeItem('tenantId'); // Clear to force re-entry
            return false;
        }
        return true;
    }

    function initIdentityWorkflow() {
        if (!nodes.modal) return;

        // Force entry modal if session is invalid or missing
        if (!isSessionValid()) {
            window.toggleIdentityModal(true);
        }

        nodes.saveBtn.addEventListener('click', () => {
            const newTenant = nodes.tenantInput.value.trim();
            if (!newTenant) {
                alert('A valid Tenant ID is required to access the platform.');
                return;
            }

            sessionSettings.tenantId = newTenant;
            sessionSettings.userRole = nodes.roleSelect.value;

            safeStorage.set('tenantId', sessionSettings.tenantId);
            safeStorage.set('userRole', sessionSettings.userRole);

            updateUIIdentity();
            window.toggleIdentityModal(false);

            if (history.length === 0) {
                appendMessage('ai', `Welcome back. Session initialized for **Tenant ${sessionSettings.tenantId}**. How can I help you?`);
            }
        });

        // Only allow closing on backdrop IF a valid session already exists
        nodes.modal.addEventListener('click', (e) => {
            if (e.target === nodes.modal && isSessionValid()) {
                window.toggleIdentityModal(false);
            }
        });

        // Activity Monitor: Reset the 1-hour timer on user interaction
        ['mousedown', 'keydown', 'scroll', 'touchstart'].forEach(type => {
            document.addEventListener(type, () => safeStorage.updateActivity(), { passive: true });
        });
    }

    // --- MESSAGING ENGINE ---
    async function sendMessage(prompt) {
        if (!prompt || !prompt.trim()) return;

        const text = prompt.trim();

        // Screen transition
        if (nodes.chatViewport.contains(nodes.welcomeScreen)) {
            nodes.chatViewport.innerHTML = '<div class="message-thread" id="messageThread"></div>';
        }

        const thread = document.getElementById('messageThread');
        if (!thread) return;

        appendMessage('user', text);
        nodes.chatInput.value = '';
        nodes.chatInput.style.height = 'auto';

        const loadingId = `load-${Date.now()}`;
        const loader = document.createElement('div');
        loader.className = 'message ai loading';
        loader.id = loadingId;
        loader.innerHTML = `
            <div class="message-icon"><i class="fas fa-robot"></i></div>
            <div class="message-content">
                <span class="thinking-label" aria-hidden="true">Thinking&hellip;</span>
            </div>
        `;
        thread.appendChild(loader);
        nodes.chatViewport.scrollTo({ top: nodes.chatViewport.scrollHeight, behavior: 'smooth' });

        try {
            const response = await fetch('/ai/chat/stream', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Tenant-ID': sessionSettings.tenantId,
                    'X-User-Timezone': sessionSettings.timezone,
                    'User-Role': sessionSettings.userRole
                },
                body: JSON.stringify({ prompt: text, history })
            });

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let accumulatedContent = "";
            let aiMessageDiv = null;
            let aiContentDiv = null;

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;

                const chunk = decoder.decode(value);
                const lines = chunk.split('\n');

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;

                    let data;
                    try {
                        data = JSON.parse(line.slice(6));
                    } catch (e) { continue; }

                    const currentLoader = document.getElementById(loadingId);
                    if (currentLoader) currentLoader.remove();

                    // Initialize AI message container on first chunk
                    if (!aiMessageDiv) {
                        aiMessageDiv = document.createElement('div');
                        aiMessageDiv.className = 'message ai';
                        aiMessageDiv.innerHTML = `
                            <div class="message-icon"><i class="fas fa-robot"></i></div>
                            <div class="message-content" id="stream-content-${loadingId}"></div>
                        `;
                        thread.appendChild(aiMessageDiv);
                        aiContentDiv = document.getElementById(`stream-content-${loadingId}`);
                    }

                    if (data.action) {
                        // Display background action progress
                        const actionEl = document.createElement('div');
                        actionEl.className = 'agent-action-note';
                        actionEl.style = 'font-style: italic; color: var(--text-secondary); font-size: 0.8rem; margin: 4px 0;';
                        actionEl.innerHTML = `<i class="fas fa-cog fa-spin"></i> ${data.action}`;
                        aiContentDiv.appendChild(actionEl);
                        nodes.chatViewport.scrollTo({ top: nodes.chatViewport.scrollHeight, behavior: 'smooth' });
                    }

                    if (data.content) {
                        accumulatedContent += data.content;
                        // Use marked if available, otherwise raw
                        aiContentDiv.innerHTML = typeof marked !== 'undefined' ? marked.parse(accumulatedContent) : accumulatedContent;
                        nodes.chatViewport.scrollTo({ top: nodes.chatViewport.scrollHeight, behavior: 'smooth' });
                    }

                    if (data.status === 'auth_required') {
                        aiContentDiv.innerHTML += `
                            <div class="auth-required-box" style="background: rgba(73, 124, 254, 0.1); padding: 20px; border-radius: 12px; border: 1px dashed var(--accent-color); margin-top: 10px;">
                                <p><strong><i class="fab fa-google"></i> Calendar Access Required</strong></p>
                                <p style="margin: 8px 0; font-size: 0.875rem;">${data.message}</p>
                                <a href="${data.auth_url}" target="_blank" class="primary-btn" style="display:inline-block; width:auto; text-decoration:none;">Authorize Connection</a>
                            </div>`;
                    }

                    if (data.done) {
                        history = data.history || [];
                        if (data.suggested_actions) {
                            // Handle suggested actions if we wanted to re-render them
                        }
                    }
                }
            }
        } catch (e) {
            console.error('Streaming error:', e);
            const currentLoader = document.getElementById(loadingId);
            if (currentLoader) currentLoader.remove();
            appendMessage('ai', 'Connection lost. Please try again.');
        }
    }

    function appendMessage(role, content) {
        let thread = document.getElementById('messageThread');
        if (!thread) {
            nodes.chatViewport.innerHTML = '<div class="message-thread" id="messageThread"></div>';
            thread = document.getElementById('messageThread');
        }

        const div = document.createElement('div');
        div.className = `message ${role}`;

        const icon = role === 'ai' ? '<i class="fas fa-robot"></i>' : '<i class="fas fa-user"></i>';
        const formatted = role === 'ai' ? (typeof marked !== 'undefined' ? marked.parse(content) : content) : content;

        div.innerHTML = `
            <div class="message-icon">${icon}</div>
            <div class="message-content">${formatted}</div>
        `;

        thread.appendChild(div);
        nodes.chatViewport.scrollTo({ top: nodes.chatViewport.scrollHeight, behavior: 'smooth' });
    }

    // --- EVENT REGISTRATION ---
    nodes.chatInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = `${this.scrollHeight}px`;
    });

    nodes.sidebarToggle.addEventListener('click', () => nodes.sidebar.classList.toggle('open'));
    nodes.mobileClose.addEventListener('click', () => nodes.sidebar.classList.remove('open'));

    nodes.newChatBtn.addEventListener('click', () => {
        nodes.chatViewport.innerHTML = '';
        nodes.chatViewport.appendChild(nodes.welcomeScreen);
        history = [];
        nodes.chatInput.value = '';
    });

    nodes.starterChips.forEach(chip => {
        chip.addEventListener('click', (e) => {
            e.preventDefault();
            const prompt = chip.getAttribute('data-prompt');
            if (prompt) {
                sendMessage(prompt);
                nodes.sidebar.classList.remove('open');
            }
        });
    });

    nodes.chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage(nodes.chatInput.value);
        }
    });

    nodes.sendBtn.addEventListener('click', () => sendMessage(nodes.chatInput.value));

    function initThemeWorkflow() {
        if (!nodes.themeToggle) return;

        const icon = nodes.themeToggle.querySelector('i');
        const updateIcon = (isLight) => {
            if (isLight) {
                icon.className = 'fas fa-sun';
            } else {
                icon.className = 'fas fa-moon';
            }
        };

        // Sync icon on startup
        updateIcon(document.documentElement.classList.contains('light-mode'));

        nodes.themeToggle.addEventListener('click', () => {
            const isLight = document.documentElement.classList.toggle('light-mode');
            safeStorage.set('theme', isLight ? 'light' : 'dark');
            updateIcon(isLight);
        });
    }

    // INITIALIZE
    updateUIIdentity();
    initIdentityWorkflow();
    initThemeWorkflow();
});
