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
        emailInput: document.getElementById('emailInput'),
        timezoneInput: document.getElementById('timezoneInput'),
        saveBtn: document.getElementById('saveSettingsBtn'),
        themeToggle: document.getElementById('themeToggle'),
        // Intelligence Extensions
        draftSidebar: document.getElementById('draftSidebar'),
        progressList: document.getElementById('progressList'),
        activeWorkflowBadge: document.getElementById('activeWorkflowBadge'),
        suggestedChipsBar: document.getElementById('suggestedChipsBar')
    };

    const friendlyActionLabels = {
        'create_contact': 'Updating contact draft...',
        'create_client_record': 'Updating client record...',
        'create_matter': 'Drafting legal matter...',
        'create_standard_event': 'Syncing calendar entry...',
        'create_all_day_event': 'Setting deadline in calendar...',
        'lookup_countries': 'Resolving country code...',
        'lookup_client': 'Validating client identity...',
        'lookup_practice_area': 'Categorizing practice area...',
        'lookup_case_stage': 'Determining case status...',
        'lookup_billing_type': 'Aligning billing protocol...',
        'search_contact_by_email': 'Searching database...'
    };

    function updateDraftSidebar(progressMeta, workflowId) {
        if (!nodes.draftSidebar || !nodes.progressList) return;

        if (!progressMeta || progressMeta.length === 0) {
            nodes.activeWorkflowBadge.textContent = 'No active workflow';
            nodes.activeWorkflowBadge.style.opacity = '0.5';
            nodes.progressList.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-wand-magic-sparkles"></i>
                    <p>Start a workflow to see progress here</p>
                </div>
            `;
            return;
        }

        nodes.activeWorkflowBadge.textContent = workflowId ? workflowId.replace(/_/g, ' ') : 'In Progress';
        nodes.activeWorkflowBadge.style.opacity = '1';

        nodes.progressList.innerHTML = progressMeta.map(field => `
            <div class="progress-item ${field.status}">
                <div class="progress-icon">
                    <i class="fas ${field.status === 'captured' ? 'fa-check' : (field.status === 'pending' ? 'fa-pen-to-square' : 'fa-circle-dot')}"></i>
                </div>
                <div class="progress-info">
                    <div class="progress-label">${field.label}</div>
                    <div class="progress-value">${field.value || (field.status === 'pending' ? '<i>waiting...</i>' : '—')}</div>
                </div>
            </div>
        `).join('');
    }

    function renderSuggestedChips(chips) {
        if (!nodes.suggestedChipsBar) return;
        
        nodes.suggestedChipsBar.innerHTML = '';
        if (!chips || chips.length === 0) return;

        chips.forEach(chip => {
            const btn = document.createElement('button');
            btn.className = 'action-chip';
            btn.textContent = chip.label;
            btn.addEventListener('click', () => sendMessage(chip.prompt));
            nodes.suggestedChipsBar.appendChild(btn);
        });
    }

    window.app = {
        clearCurrentWorkflow: () => {
             sendMessage("/clear");
             updateDraftSidebar([], null);
             renderSuggestedChips([]);
        }
    };

    function generateUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    // --- SESSION IDENTITY STATE ---
    const sessionSettings = {
        tenantId: safeStorage.get('tenantId', null),
        userRole: safeStorage.get('userRole', 'Associate'),
        userEmail: safeStorage.get('userEmail', ''),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        threadId: safeStorage.get('threadId', generateUUID())
    };
    safeStorage.set('threadId', sessionSettings.threadId);

    let history = [];

    function updateUIIdentity() {
        if (nodes.badgeTenant) nodes.badgeTenant.textContent = sessionSettings.tenantId;
        if (nodes.tenantInput) nodes.tenantInput.value = sessionSettings.tenantId;
        if (nodes.roleSelect) nodes.roleSelect.value = sessionSettings.userRole;
        if (nodes.emailInput) nodes.emailInput.value = sessionSettings.userEmail;
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
            sessionSettings.userEmail = nodes.emailInput.value.trim();

            safeStorage.set('tenantId', sessionSettings.tenantId);
            safeStorage.set('userRole', sessionSettings.userRole);
            safeStorage.set('userEmail', sessionSettings.userEmail);

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
                    'X-User-Email': sessionSettings.userEmail,
                    'X-User-Timezone': sessionSettings.timezone,
                    'User-Role': sessionSettings.userRole
                },
                body: JSON.stringify({ prompt: text, history, thread_id: sessionSettings.threadId })
            });
            console.log(`[DEBUG] Sending request with headers:`, {
                'X-Tenant-ID': sessionSettings.tenantId,
                'X-User-Email': sessionSettings.userEmail
            });

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let accumulatedContent = "";
            let aiMessageDiv = null;
            let aiContentDiv = null;

            let streamBuffer = "";
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;

                streamBuffer += decoder.decode(value, { stream: true });
                const streamLines = streamBuffer.split('\n');
                streamBuffer = streamLines.pop(); // Keep partial line in buffer

                for (const line of streamLines) {
                    const trimmed = line.trim();
                    if (!trimmed || !trimmed.startsWith('data: ')) continue;

                    let data;
                    try {
                        data = JSON.parse(trimmed.slice(6));
                    } catch (e) {
                        console.error("[SSE-PARSE-ERROR] Fragmented JSON or invalid data:", line);
                        continue;
                    }

                    const currentLoader = document.getElementById(loadingId);
                    if (currentLoader) currentLoader.remove();

                    // [INTELLIGENCE]: Handle Action Labels (Tool execution)
                    if (data.action) {
                        const toolName = data.action.replace('Executing ', '').replace('...', '');
                        const friendly = friendlyActionLabels[toolName] || data.action;
                        
                        // We show a temporary status bubble or update the thinking label
                        let actionLabel = document.getElementById(`action-${loadingId}`);
                        if (!actionLabel) {
                            actionLabel = document.createElement('div');
                            actionLabel.id = `action-${loadingId}`;
                            actionLabel.className = 'action-status';
                            thread.appendChild(actionLabel);
                        }
                        actionLabel.innerHTML = `<i class="fas fa-cog fa-spin"></i> ${friendly}`;
                        nodes.chatViewport.scrollTo({ top: nodes.chatViewport.scrollHeight, behavior: 'smooth' });
                    }

                    // [INTELLIGENCE]: Handle Progress Meta & Chips
                    if (data.progress_meta) {
                        updateDraftSidebar(data.progress_meta, data.workflow_id);
                    }
                    if (data.suggested_chips) {
                        renderSuggestedChips(data.suggested_chips);
                    }
                    if (data.vault_data) {
                        // Sync UI with latest vault if needed (e.g. name changed)
                        updateDraftSidebar(data.vault_data.progress_meta || [], data.vault_data.workflow_id);
                    }

                    // Initialize AI message container on first content chunk
                    if (data.content && !aiMessageDiv) {
                        // Clear any temporary action labels when content starts
                        const actionLabel = document.getElementById(`action-${loadingId}`);
                        if (actionLabel) actionLabel.remove();

                        aiMessageDiv = document.createElement('div');
                        aiMessageDiv.className = 'message ai';
                        aiMessageDiv.innerHTML = `
                            <div class="message-icon"><i class="fas fa-robot"></i></div>
                            <div class="message-content" id="stream-content-${loadingId}"></div>
                        `;
                        thread.appendChild(aiMessageDiv);
                        aiContentDiv = document.getElementById(`stream-content-${loadingId}`);
                    }

                    if (data.content && aiContentDiv) {
                        accumulatedContent += data.content;
                        // Use a markdown parser or simple formatter
                        aiContentDiv.innerHTML = typeof marked !== 'undefined' ? marked.parse(accumulatedContent) : accumulatedContent;
                        nodes.chatViewport.scrollTo({ top: nodes.chatViewport.scrollHeight, behavior: 'smooth' });
                    }

                    if (data.status === 'auth_required') {
                        const isMatterMiner = data.auth_type === 'matterminer_core' ||
                            (data.message && data.message.includes('MatterMiner'));

                        if (isMatterMiner) {
                            aiContentDiv.innerHTML += `
                                <div class="auth-required-box core-login-box" style="background: rgba(16, 185, 129, 0.1); padding: 20px; border-radius: 12px; border: 1px dashed #10b981; margin-top: 10px;">
                                    <p><strong><i class="fas fa-lock"></i> MatterMiner Core Login</strong></p>
                                    <p style="margin: 8px 0; font-size: 0.875rem;">${data.message || 'Please log in to your MatterMiner account to proceed.'}</p>
                                    <div class="login-form" style="display: flex; flex-direction: column; gap: 8px; margin-top: 12px;">
                                        <input type="email" class="login-email" placeholder="Email" style="padding: 10px; border-radius: 8px; border: 1px solid var(--border-color); background: var(--bg-secondary); color: var(--text-primary);">
                                        <input type="password" class="login-password" placeholder="Password" style="padding: 10px; border-radius: 8px; border: 1px solid var(--border-color); background: var(--bg-secondary); color: var(--text-primary);">
                                        <button class="primary-btn core-login-submit-btn" style="display:inline-block; width:auto; border:none; cursor:pointer; padding: 10px 20px; border-radius: 8px; background: #10b981; color: white;">Log In</button>
                                    </div>
                                </div>`;
                        } else {
                            // Default to Google/Calendar for google_calendar or fallback
                            aiContentDiv.innerHTML += `
                                <div class="auth-required-box" style="background: rgba(73, 124, 254, 0.1); padding: 20px; border-radius: 12px; border: 1px dashed var(--accent-color); margin-top: 10px;">
                                    <p><strong><i class="fab fa-google"></i> Calendar Access Required</strong></p>
                                    <p style="margin: 8px 0; font-size: 0.875rem;">${data.message || 'Google Calendar connection is required to schedule events.'}</p>
                                    <button class="primary-btn oauth-trigger-btn" data-url="${data.auth_url || ''}" style="display:inline-block; width:auto; border:none; cursor:pointer; padding: 10px 20px; border-radius: 8px;">Authorize Connection</button>
                                </div>`;
                        }
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
    // OAuth Button Delegation
    nodes.chatViewport.addEventListener('click', async (e) => {
        if (e.target.classList.contains('oauth-trigger-btn')) {
            const targetUrl = e.target.getAttribute('data-url');
            if (!targetUrl) return;

            // 1. Instantly open tab to bypass pop-up blockers
            const authWindow = window.open('', '_blank');
            authWindow.document.write('<div style="font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh;">Loading Google Authorization...</div>');

            try {
                // 2. Fetch the JSON from Node.js
                const response = await fetch(targetUrl);
                const payload = await response.json();

                // 3. Extract the `data` attribute
                if (payload.success && payload.data) {
                    // 4. Send the ALREADY OPEN tab to Google Consent
                    authWindow.location.href = payload.data;
                } else {
                    throw new Error('Failed to parse URL payload.');
                }
            } catch (error) {
                console.error("Authorization fetch failed:", error);
                authWindow.document.write('<div style="color: red; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh;">Error opening authorization window. Please try again.</div>');
            }
        }

        // MatterMiner Core Login Delegation
        if (e.target.classList.contains('core-login-submit-btn')) {
            const container = e.target.closest('.core-login-box');
            const email = container.querySelector('.login-email').value;
            const password = container.querySelector('.login-password').value;

            if (!email || !password) {
                alert('Please provide both email and password.');
                return;
            }

            // We simulate a message to the agent that triggers the login tool
            // The agent already has the tool authenticate_to_core(email, password)
            const prompt = `Please log me in with email ${email} and password ${password}`;

            // Disable the form to show progress
            e.target.disabled = true;
            e.target.textContent = 'Logging in...';

            // Auto-update identity so subsequent requests have the header
            sessionSettings.userEmail = email;
            safeStorage.set('userEmail', email);
            updateUIIdentity();

            sendMessage(prompt);
        }
    });

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
        sessionSettings.threadId = generateUUID();
        safeStorage.set('threadId', sessionSettings.threadId);
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