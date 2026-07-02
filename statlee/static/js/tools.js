/* Workspace tools: project export (5.3), AI report builder (5.17), analysis
 * history (5.7), analysis catalog (5.14), issue reporting (6.3), accounts
 * (7.1), and the auth boot sequence. */
(function () {
    'use strict';

    // =====================================================================
    // Project export (5.3)
    // =====================================================================
    async function exportProject() {
        if (!CC.state.filename && !CC.getCode()) {
            CC.toast('Nothing to export yet — upload data or generate code first.', 'error');
            return;
        }
        const btn = document.getElementById('exportBtn');
        btn.disabled = true;
        try {
            const res = await fetch('/export', {
                method: 'POST',
                headers: CC.headers(),
                body: JSON.stringify({
                    filename: CC.state.filename,
                    code: CC.getCode(),
                    language: document.getElementById('languageSelect').value,
                    history: CC.state.chatHistory,
                    interpretation: CC.state.latestInterpretation,
                    report: CC.state.report,
                }),
            });
            if (!res.ok) {
                let msg = 'Export failed.';
                try { msg = (await res.json()).error || msg; } catch (e) { /* binary */ }
                CC.toast(msg, 'error');
                return;
            }
            const blob = await res.blob();
            const stem = (CC.state.filename || 'project').replace(/\.[^.]+$/, '');
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `Project_${stem}.zip`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(a.href);
            CC.toast('Project archive downloaded.', 'success');
        } catch (e) {
            CC.toast('Network error during export.', 'error');
        } finally {
            btn.disabled = false;
        }
    }

    // =====================================================================
    // AI report builder (5.17)
    // =====================================================================
    let reportPreviewOn = false;

    async function generateReport() {
        const btn = document.getElementById('reportGenerateBtn');
        const editor = document.getElementById('reportEditor');
        if (!CC.state.lastRun || !CC.state.lastRun.output) {
            CC.toast('Run an analysis first — the report is grounded in actual results.', 'error');
            return;
        }
        btn.disabled = true;
        btn.innerHTML = `${CC.spinner('h-4 w-4')} Writing...`;
        try {
            const res = await CC.postStream('/generate_report', {
                background: document.getElementById('reportContext').value,
                length: document.getElementById('reportLength').value,
                tone: document.getElementById('reportTone').value,
                format: document.getElementById('reportFormat').value,
                output: CC.state.lastRun.output,
                interpretation: CC.state.latestInterpretation,
                history: CC.state.chatHistory,
                converse: CC.state.converseHistory,
            });
            const ct = res.headers.get('Content-Type') || '';
            if (!ct.includes('text/event-stream')) {
                const data = await res.json();
                CC.toast(data.error || 'Report generation failed.', 'error');
                return;
            }
            editor.value = '';
            setPreview(false);
            let streamError = null;
            await CC.streamSSE(res, {
                onDelta: (t) => {
                    editor.value += t;
                    editor.scrollTop = editor.scrollHeight;
                },
                onDone: (d) => { if (d.usage) CC.addUsage(d.usage); },
                onError: (m) => { streamError = m; },
            });
            if (streamError) CC.toast(streamError, 'error');
            CC.state.report = editor.value;
        } catch (e) {
            CC.toast('Network error generating the report.', 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = 'Generate Report';
        }
    }

    async function reviseSelection() {
        const editor = document.getElementById('reportEditor');
        const start = editor.selectionStart, end = editor.selectionEnd;
        const selection = editor.value.slice(start, end);
        if (!selection.trim()) {
            CC.toast('Select the passage you want revised first (in the editor view).', 'error');
            return;
        }
        const instruction = window.prompt(
            'How should this passage change? (e.g. "expand with more detail", "make it more formal")');
        if (!instruction) return;
        const btn = document.getElementById('reportReviseBtn');
        btn.disabled = true;
        btn.innerHTML = CC.spinner('h-4 w-4');
        try {
            const res = await CC.postStream('/generate_report', {
                revision: { report: editor.value, selection, instruction },
            });
            const ct = res.headers.get('Content-Type') || '';
            if (!ct.includes('text/event-stream')) {
                const data = await res.json();
                CC.toast(data.error || 'Revision failed.', 'error');
                return;
            }
            let revised = '';
            let streamError = null;
            await CC.streamSSE(res, {
                onDelta: (t) => { revised += t; },
                onDone: (d) => { if (d.usage) CC.addUsage(d.usage); },
                onError: (m) => { streamError = m; },
            });
            if (streamError) { CC.toast(streamError, 'error'); return; }
            editor.value = editor.value.slice(0, start) + revised.trim() + editor.value.slice(end);
            CC.state.report = editor.value;
            CC.toast('Passage revised.', 'success');
        } catch (e) {
            CC.toast('Network error during revision.', 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = 'Revise selection';
        }
    }

    function setPreview(on) {
        reportPreviewOn = on;
        const editor = document.getElementById('reportEditor');
        const preview = document.getElementById('reportPreview');
        const toggle = document.getElementById('reportPreviewBtn');
        if (on) {
            preview.innerHTML = CC.renderMarkdown(editor.value);
            preview.classList.remove('hidden');
            editor.classList.add('hidden');
            toggle.textContent = 'Edit';
        } else {
            preview.classList.add('hidden');
            editor.classList.remove('hidden');
            toggle.textContent = 'Preview';
        }
    }

    function downloadReport() {
        const text = document.getElementById('reportEditor').value;
        if (!text.trim()) { CC.toast('No report to download yet.', 'error'); return; }
        const blob = new Blob([text], { type: 'text/markdown' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'report.md';
        a.click();
        URL.revokeObjectURL(a.href);
    }

    // =====================================================================
    // Analysis history (5.7) — localStorage always; server when logged in
    // =====================================================================
    const HISTORY_KEY = 'cc_history';

    CC.recordHistory = function (entry) {
        entry.ts = Date.now();
        try {
            const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
            list.unshift(entry);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, 30)));
        } catch (e) { /* storage full/disabled */ }
        CC.state.report = CC.state.report || '';
        CC.post('/history', entry).catch(() => { /* anonymous: not persisted */ });
    };

    async function openHistory() {
        const list = document.getElementById('historyList');
        list.innerHTML = `<div class="p-4 text-sm text-slate-500 flex items-center gap-2">${CC.spinner('h-4 w-4')} Loading…</div>`;
        CC.modal.open('historyModal');

        let entries = [];
        try { entries = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch (e) { /* ignore */ }
        let serverNote = '';
        try {
            const { ok, data } = await fetch('/history', { headers: { 'X-CSRF-Token': CC.csrfToken } })
                .then(r => r.json().then(d => ({ ok: r.ok, data: d })));
            if (ok && data.persisted && data.runs.length) {
                const seen = new Set(entries.map(e => (e.code || '') + (e.ts || '')));
                data.runs.forEach(r => {
                    const ts = r.created_at ? Date.parse(r.created_at) : 0;
                    if (seen.has((r.code || '') + ts)) return;
                    entries.push({
                        prompt: r.prompt, code: r.code, language: r.language,
                        dataset_name: r.dataset_name, output: r.output,
                        interpretation: r.interpretation,
                        ts, server: true,
                    });
                });
                serverNote = '<p class="px-4 pb-2 text-[10px] font-mono uppercase tracking-widest text-emerald-600 dark:text-emerald-400">Account history synced</p>';
            }
        } catch (e) { /* anonymous */ }

        entries.sort((a, b) => (b.ts || 0) - (a.ts || 0));
        if (!entries.length) {
            list.innerHTML = '<div class="p-6 text-sm text-slate-500 italic">No past analyses yet. Run a script and it will be saved here.</div>';
            return;
        }
        list.innerHTML = serverNote;
        entries.slice(0, 40).forEach(entry => {
            const item = document.createElement('button');
            item.className = 'w-full text-left px-4 py-3 border-b border-slate-200/60 dark:border-slate-700/40 hover:bg-indigo-50/50 dark:hover:bg-indigo-900/10 transition-colors';
            const when = entry.ts ? new Date(entry.ts).toLocaleString() : '';
            item.innerHTML =
                `<div class="text-sm font-semibold text-slate-800 dark:text-slate-200 truncate">${CC.escapeHtml(entry.prompt || '(no prompt)')}</div>` +
                `<div class="text-[11px] font-mono text-slate-500 mt-0.5">${CC.escapeHtml(entry.dataset_name || '')} · ${CC.escapeHtml(entry.language || '')} · ${when}${entry.server ? ' · account' : ''}</div>`;
            item.addEventListener('click', () => restoreHistory(entry));
            list.appendChild(item);
        });
    }

    function restoreHistory(entry) {
        CC.modal.close('historyModal');
        if (entry.code) {
            CC.setCode(entry.code);
            document.getElementById('copyBtn').style.display = 'flex';
            document.getElementById('runSection').classList.remove('hidden');
        }
        if (entry.language) {
            document.getElementById('languageSelect').value = entry.language;
            document.getElementById('languageSelect').dispatchEvent(new Event('change'));
        }
        if (entry.prompt) document.getElementById('promptInput').value = entry.prompt;
        if (entry.output) document.getElementById('runOutput').innerText = entry.output;
        if (entry.interpretation) {
            CC.state.latestInterpretation = entry.interpretation;
            document.getElementById('interpretationOutput').innerHTML =
                CC.renderMarkdown(entry.interpretation);
        }
        CC.switchTab('generate');
        CC.toast('Past analysis restored. Re-upload the dataset to re-run it.', 'info');
    }

    // =====================================================================
    // Analysis catalog (5.14)
    // =====================================================================
    const CATALOG = [
        {
            group: 'Comparing groups',
            methods: [
                { name: 'Independent-samples t-test', description: 'Compare the mean of a continuous variable between two groups' },
                { name: 'Paired t-test', description: 'Compare two related measurements on the same units' },
                { name: 'One-way ANOVA', description: 'Compare a continuous variable across 3+ groups' },
                { name: 'Mann-Whitney U test', description: 'Non-parametric two-group comparison for ordinal/skewed data' },
                { name: 'Kruskal-Wallis test', description: 'Non-parametric comparison across 3+ groups' },
            ],
        },
        {
            group: 'Relationships',
            methods: [
                { name: 'Pearson correlation', description: 'Linear association between two continuous variables' },
                { name: 'Spearman rank correlation', description: 'Monotonic association for ordinal or non-normal data' },
                { name: 'Chi-square test of independence', description: 'Association between two categorical variables' },
            ],
        },
        {
            group: 'Regression & prediction',
            methods: [
                { name: 'OLS linear regression', description: 'Predict a continuous outcome from one or more predictors' },
                { name: 'Multiple regression with controls', description: 'Estimate an effect while controlling for covariates' },
                { name: 'Logistic regression', description: 'Predict a binary outcome; odds ratios' },
                { name: 'Ordinal regression', description: 'Predict an ordered categorical outcome (e.g., Likert)' },
            ],
        },
        {
            group: 'Visualization & description',
            methods: [
                { name: 'Descriptive summary', description: 'Means, medians, distributions, and missingness overview' },
                { name: 'Histogram / density plots', description: 'Shape of a continuous variable\'s distribution' },
                { name: 'Box plots by group', description: 'Distribution of a continuous variable across categories' },
                { name: 'Scatter plot with regression line', description: 'Bivariate relationship with fitted trend' },
                { name: 'Correlation heatmap', description: 'Pairwise correlations across numeric variables' },
            ],
        },
    ];

    function renderCatalog() {
        const wrap = document.getElementById('catalogList');
        wrap.innerHTML = '';
        CATALOG.forEach(group => {
            const h = document.createElement('div');
            h.className = 'px-1 pt-4 pb-1 text-[10px] font-mono font-bold uppercase tracking-[0.2em] text-slate-500 dark:text-slate-400';
            h.textContent = group.group;
            wrap.appendChild(h);
            group.methods.forEach(m => {
                const btn = document.createElement('button');
                btn.className = 'w-full text-left px-4 py-3 rounded-xl border border-slate-300 dark:border-slate-700/50 bg-white/70 dark:bg-surface-2/50 hover:border-indigo-400 dark:hover:border-indigo-500/50 hover:shadow-md transition-all mb-2';
                btn.innerHTML =
                    `<div class="text-sm font-bold text-indigo-900 dark:text-indigo-300">${CC.escapeHtml(m.name)}</div>` +
                    `<div class="text-xs text-slate-600 dark:text-slate-400 mt-0.5">${CC.escapeHtml(m.description)}</div>`;
                btn.addEventListener('click', () => pickMethod(m, btn));
                wrap.appendChild(btn);
            });
        });
    }

    async function pickMethod(method, btn) {
        if (!CC.state.filename) {
            CC.toast('Upload a dataset first so I can match the method to your columns.', 'error');
            return;
        }
        const orig = btn.innerHTML;
        btn.innerHTML = `<div class="flex items-center gap-2 text-sm font-bold text-indigo-700 dark:text-indigo-300">${CC.spinner('h-4 w-4')} ${CC.escapeHtml(method.name)}? Matching it to your data…</div>`;
        try {
            const { ok, data } = await CC.post('/method_prompt', {
                filename: CC.state.filename,
                method,
                codebook: CC.state.codebook,
                pdf_mapping: CC.state.pdfMapping,
            });
            if (ok && data.prompt) {
                if (data.usage) CC.addUsage(data.usage);
                document.getElementById('promptInput').value = data.prompt;
                CC.modal.close('catalogModal');
                CC.toast(data.rationale || 'Prompt drafted — review and hit Generate.', 'success');
            } else {
                CC.toast((data && data.error) || 'Could not draft a prompt for that method.', 'error');
            }
        } catch (e) {
            CC.toast('Network error.', 'error');
        } finally {
            btn.innerHTML = orig;
        }
    }

    // =====================================================================
    // Issue reporting (6.3)
    // =====================================================================
    async function submitIssue() {
        const desc = document.getElementById('issueDescription').value.trim();
        if (!desc) { CC.toast('Please describe the issue.', 'error'); return; }
        const btn = document.getElementById('issueSubmitBtn');
        btn.disabled = true;
        try {
            const { ok, data } = await CC.post('/report_issue', {
                description: desc,
                code: CC.getCode(),
                output: CC.state.lastRun ? CC.state.lastRun.output : '',
                console_errors: CC.consoleErrors.join('\n'),
            });
            if (ok) {
                CC.modal.close('issueModal');
                document.getElementById('issueDescription').value = '';
                CC.toast(`Thanks! Issue #${data.id} was filed with full diagnostics.`, 'success');
            } else {
                CC.toast((data && data.error) || 'Could not submit the report.', 'error');
            }
        } catch (e) {
            CC.toast('Network error submitting the report.', 'error');
        } finally {
            btn.disabled = false;
        }
    }

    // =====================================================================
    // Accounts (7.1) + auth boot
    // =====================================================================
    function setAccountUI(user) {
        CC.state.user = user;
        const label = document.getElementById('accountLabel');
        const loginItem = document.getElementById('accountLoginItem');
        const logoutItem = document.getElementById('accountLogoutItem');
        if (user) {
            label.textContent = user.email;
            label.classList.remove('hidden');
            loginItem.classList.add('hidden');
            logoutItem.classList.remove('hidden');
        } else {
            label.classList.add('hidden');
            loginItem.classList.remove('hidden');
            logoutItem.classList.add('hidden');
        }
    }

    async function doLogin() {
        const email = document.getElementById('accEmail').value.trim();
        const password = document.getElementById('accPassword').value;
        const err = document.getElementById('accError');
        err.classList.add('hidden');
        const { ok, data } = await CC.post('/login', { email, password });
        if (ok) { window.location.reload(); }
        else { err.textContent = data.error || 'Login failed.'; err.classList.remove('hidden'); }
    }

    async function doRegister() {
        const email = document.getElementById('accEmail').value.trim();
        const password = document.getElementById('accPassword').value;
        const err = document.getElementById('accError');
        err.classList.add('hidden');
        const { ok, data } = await CC.post('/register', { email, password });
        if (ok) { window.location.reload(); }
        else { err.textContent = data.error || 'Registration failed.'; err.classList.remove('hidden'); }
    }

    async function doLogout() {
        await CC.post('/logout', {});
        window.location.reload();
    }

    async function checkAuth() {
        try {
            const res = await fetch('/check_auth');
            const data = await res.json();
            if (res.ok) {
                setAccountUI(data.user);
                return;
            }
            if (data.mode === 'accounts') {
                const m = document.getElementById('accountModal');
                m.dataset.forced = '1';
                CC.modal.open('accountModal');
            } else {
                document.getElementById('authOverlay').classList.remove('hidden');
            }
        } catch (e) {
            console.error('Auth check failed:', e);
        }
    }

    function initLegacyPasswordOverlay() {
        const authBtn = document.getElementById('authBtn');
        authBtn.addEventListener('click', async () => {
            const pwd = document.getElementById('authPassword').value;
            const errorEl = document.getElementById('authError');
            authBtn.disabled = true;
            authBtn.innerHTML = '<span class="animate-pulse">Verifying...</span>';
            errorEl.classList.add('hidden');
            try {
                const { ok } = await CC.post('/login', { password: pwd });
                if (ok) {
                    const overlay = document.getElementById('authOverlay');
                    overlay.classList.add('opacity-0');
                    setTimeout(() => overlay.classList.add('hidden'), 500);
                } else {
                    errorEl.classList.remove('hidden');
                    document.getElementById('authPassword').value = '';
                    document.getElementById('authPassword').focus();
                }
            } catch (e) {
                errorEl.innerText = 'Network error connecting to server.';
                errorEl.classList.remove('hidden');
            } finally {
                authBtn.disabled = false;
                authBtn.innerText = 'Unlock System';
            }
        });
        document.getElementById('authPassword').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') authBtn.click();
        });
    }

    // =====================================================================
    // Boot
    // =====================================================================
    document.addEventListener('DOMContentLoaded', () => {
        checkAuth();
        initLegacyPasswordOverlay();
        renderCatalog();

        document.getElementById('exportBtn').addEventListener('click', exportProject);
        document.getElementById('historyBtn').addEventListener('click', openHistory);
        document.getElementById('catalogBtn').addEventListener('click', () => CC.modal.open('catalogModal'));
        document.getElementById('issueBtn').addEventListener('click', () => CC.modal.open('issueModal'));
        document.getElementById('issueSubmitBtn').addEventListener('click', submitIssue);

        document.getElementById('reportGenerateBtn').addEventListener('click', generateReport);
        document.getElementById('reportReviseBtn').addEventListener('click', reviseSelection);
        document.getElementById('reportPreviewBtn').addEventListener('click', () => setPreview(!reportPreviewOn));
        document.getElementById('reportDownloadBtn').addEventListener('click', downloadReport);
        document.getElementById('reportEditor').addEventListener('input', (e) => {
            CC.state.report = e.target.value;
        });

        if (window.CC_BOOT && window.CC_BOOT.accounts) {
            document.getElementById('accountBtn').addEventListener('click', () => CC.modal.open('accountModal'));
            document.getElementById('accLoginBtn').addEventListener('click', doLogin);
            document.getElementById('accRegisterBtn').addEventListener('click', doRegister);
            document.getElementById('accLogoutBtn').addEventListener('click', doLogout);
        } else {
            const accountBtn = document.getElementById('accountBtn');
            if (accountBtn) accountBtn.classList.add('hidden');
        }
    });
}());
