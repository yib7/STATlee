/* Workspace tools: project export, Report builder, analysis
 * history, analysis catalog, issue reporting, accounts
 * and the auth boot sequence. */
(function () {
    'use strict';

    // =====================================================================
    // Project export
    // =====================================================================
    function openExportDialog() {
        if (!CC.state.filename && !CC.getCode()) {
            CC.toast('Nothing to export yet. Upload data or generate code first.', 'error');
            return;
        }
        CC.modal.open('exportModal');
    }

    async function exportProject() {
        if (!CC.state.filename && !CC.getCode()) {
            CC.toast('Nothing to export yet. Upload data or generate code first.', 'error');
            return;
        }
        const btn = document.getElementById('exportDownloadBtn');
        btn.disabled = true;
        const code = CC.getCode();
        // Bundle options from the export dialog.
        const promptsBox = document.getElementById('exportIncludePrompts');
        const runsSelect = document.getElementById('exportRuns');
        const includePrompts = promptsBox ? promptsBox.checked : true;
        const runs = runsSelect ? runsSelect.value : 'all';
        try {
            const res = await fetch('/export', {
                method: 'POST',
                headers: CC.headers(),
                body: JSON.stringify({
                    filename: CC.state.filename,
                    code,
                    // Approval is per (language, code). The generated
                    // script is exported in the language it was approved in,
                    // even if the selector moved since (as Run does).
                    language: CC.runLanguage ? CC.runLanguage(code)
                        : document.getElementById('languageSelect').value,
                    // No interpretation: the server writes the run's own
                    // stored one into the bundle.
                    history: CC.state.chatHistory,
                    report: CC.state.report,
                    include_prompts: includePrompts,
                    runs,
                }),
            });
            if (!res.ok) {
                let msg = 'Export failed.';
                try { msg = (await res.json()).error || msg; } catch (e) { /* binary */ }
                CC.toast(msg, 'error');
                return;
            }
            const blob = await res.blob();
            // The server names the archive; fall back to the stored stem.
            const disposition = res.headers.get('Content-Disposition') || '';
            const named = /filename="?([^";]+)"?/i.exec(disposition);
            const stem = (CC.state.filename || 'project').replace(/\.[^.]+$/, '');
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = named ? named[1] : `Project_${stem}.zip`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            // Revoking synchronously can cancel the download in some
            // browsers before it has started reading the blob.
            setTimeout(() => URL.revokeObjectURL(a.href), 60000);
            CC.modal.close('exportModal');
            // The zip runs as unzipped. The current data
            // sits next to the script under the name it reads, the original
            // upload is in data/, cleaning.py replays the cleaning steps.
            CC.toast('Project archive downloaded. Unzip it and run analysis.py (or analysis.R) in that folder: '
                + 'README.md lists the files, including the original upload in data/, '
                + 'the software versions and a record of every step.', 'success');
        } catch (e) {
            CC.toast('Network error during export.', 'error');
        } finally {
            btn.disabled = false;
        }
    }

    // =====================================================================
    // Report builder
    // =====================================================================
    let reportPreviewOn = false;

    async function generateReport() {
        const btn = document.getElementById('reportGenerateBtn');
        const editor = document.getElementById('reportEditor');
        // The report is grounded on the workbook runs ticked in the
        // checklist; the server reads each one's own output and interpretation.
        const inWorkbook = CC.workbook ? CC.workbook.count() : 0;
        const runIds = CC.workbook ? CC.workbook.selectedRunIds() : [];
        if (inWorkbook && !runIds.length) {
            CC.toast('Select at least one run to ground the report on.', 'error');
            return;
        }
        if (!inWorkbook && (!CC.state.lastRun || !CC.state.lastRun.output)) {
            CC.toast('Run an analysis first: the report is grounded in actual results.', 'error');
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
                run_ids: runIds.length ? runIds : undefined,
                output: CC.state.lastRun ? CC.state.lastRun.output : '',
                interpretation: CC.state.latestInterpretation,
                history: CC.state.chatHistory,
                converse: CC.state.converseHistory,
            });
            const ct = res.headers.get('Content-Type') || '';
            if (!ct.includes('text/event-stream')) {
                const data = await CC.readJSON(res);
                CC.toast(CC.errorMessage(res.status, data, 'Report generation failed'), 'error');
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
                const data = await CC.readJSON(res);
                CC.toast(CC.errorMessage(res.status, data, 'Revision failed'), 'error');
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
        setTimeout(() => URL.revokeObjectURL(a.href), 60000);   // revoke late: an early revoke can cancel the download
    }

    // =====================================================================
    // Analysis history: localStorage always; server when logged in
    // =====================================================================
    const HISTORY_KEY = 'cc_history';
    const HISTORY_MAX = 30;
    // Terminal output can be 256KB; thirty of those overflow the
    // ~5MB localStorage quota, and a swallowed quota error would silently
    // stop history from saving. Keep the tail of the output, and evict the
    // oldest entries until the write fits.
    const HISTORY_OUTPUT_CHARS = 4000;

    function readLocalHistory() {
        try {
            const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
            return Array.isArray(list) ? list : [];
        } catch (e) { return []; }
    }

    function writeLocalHistory(list) {
        let items = list.slice(0, HISTORY_MAX);
        while (items.length) {
            try {
                localStorage.setItem(HISTORY_KEY, JSON.stringify(items));
                return true;
            } catch (e) {
                items = items.slice(0, items.length - 1);   // drop the oldest
            }
        }
        return false;
    }

    CC.recordHistory = function (entry) {
        entry.ts = Date.now();
        const local = Object.assign({}, entry);
        const out = local.output || '';
        if (out.length > HISTORY_OUTPUT_CHARS) {
            local.output = '[... earlier output trimmed ...]\n' + out.slice(-HISTORY_OUTPUT_CHARS);
        }
        const list = readLocalHistory();
        list.unshift(local);
        writeLocalHistory(list);
        CC.post('/history', entry).catch(() => { /* anonymous: not persisted */ });
    };

    /** Server timestamps are naive UTC ("2026-10-08T12:00:00"); Date.parse
     *  would read them as local time. */
    function parseServerTime(value) {
        if (!value) return 0;
        const s = String(value);
        const zoned = /[zZ]$|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z';
        const t = Date.parse(zoned);
        return isNaN(t) ? 0 : t;
    }

    function clearHistory() {
        if (!window.confirm('Clear the analysis history saved in this browser? '
            + 'Runs saved to your account are not affected.')) return;
        try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* storage disabled */ }
        CC.toast('Browser history cleared.', 'success');
        openHistory();
    }

    async function openHistory() {
        const list = document.getElementById('historyList');
        list.innerHTML = `<div class="p-4 text-sm text-[var(--text-3)] flex items-center gap-2">${CC.spinner('h-4 w-4')} Loading…</div>`;
        CC.modal.open('historyModal');

        const entries = readLocalHistory();
        let serverNote = '';
        try {
            const res = await fetch('/history', { headers: { 'X-CSRF-Token': CC.csrfToken } });
            const data = await CC.readJSON(res);
            if (res.ok && data.persisted && Array.isArray(data.runs) && data.runs.length) {
                // The server's created_at is stamped after the client's ts, so
                // an exact (code + ts) key never matched and every synced run
                // showed twice. Same script and prompt within 10 minutes is
                // the same run.
                const near = (a, b) => Math.abs(a - b) < 10 * 60 * 1000;
                data.runs.forEach(r => {
                    const ts = parseServerTime(r.created_at);
                    if (entries.some(e => !e.server && (e.code || '') === (r.code || '') &&
                        (e.prompt || '') === (r.prompt || '') && near(e.ts || 0, ts))) return;
                    entries.push({
                        prompt: r.prompt, code: r.code, language: r.language,
                        dataset_name: r.dataset_name, output: r.output,
                        interpretation: r.interpretation,
                        run_id: r.run_id, run_available: !!r.run_available,
                        ts, server: true,
                    });
                });
                serverNote = '<p class="px-4 pb-2 text-[10px] font-mono uppercase tracking-widest text-emerald-600 dark:text-emerald-400">Account history synced</p>';
            }
        } catch (e) { /* anonymous */ }

        entries.sort((a, b) => (b.ts || 0) - (a.ts || 0));
        if (!entries.length) {
            list.innerHTML = '<div class="p-6 text-sm text-[var(--text-3)] italic">No past analyses yet. Run a script and it will be saved here.</div>';
            return;
        }
        list.innerHTML = serverNote;
        entries.slice(0, 40).forEach(entry => {
            const item = document.createElement('button');
            item.className = 'w-full text-left px-4 py-3 border-b border-slate-200/60 dark:border-slate-700/40 hover:bg-indigo-50/50 dark:hover:bg-indigo-900/10 transition-colors';
            const when = entry.ts ? new Date(entry.ts).toLocaleString() : '';
            item.innerHTML =
                `<div class="text-sm font-semibold text-slate-800 dark:text-slate-200 truncate">${CC.escapeHtml(entry.prompt || '(no prompt)')}</div>` +
                `<div class="text-[11px] font-mono text-[var(--text-3)] mt-0.5">${CC.escapeHtml(entry.dataset_label || entry.dataset_name || '')} ·${CC.escapeHtml(entry.language || '')} · ${when}${entry.server ? ' · account' : ''}</div>`;
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
        if (entry.prompt) {
            document.getElementById('promptInput').value = entry.prompt;
            CC.state.lastPrompt = entry.prompt;
        }
        if (entry.output) {
            // Results now shows this entry, not a workbook run opened from
            // the rail (restoreSavedRun re-links it when its run still exists).
            if (CC.workbook) CC.workbook.reset();
            document.getElementById('runOutput').innerText = entry.output;
            // A history row holds no plots: the previous run's must not
            // linger beside this one's text (restoreSavedRun adds the run's
            // own plots back while its workbook run exists).
            const plotOutput = document.getElementById('plotOutput');
            if (plotOutput) plotOutput.innerHTML = '';
            // Nor its Methods card: a text-only row has none.
            // restoreSavedRun shows the run's own card while it exists.
            if (CC.clearMethods) CC.clearMethods();
            // The report builder and Converse read lastRun; without
            // it the restored results would look present but "Run an analysis
            // first" would block the report.
            // Newer entries carry the server's verdict (/interpret `debug`,
            // from the real exit code); entries without it are assumed fine.
            CC.state.lastRun = { output: entry.output, plots: [], success: entry.success !== false,
                code: entry.code || '', restored: true };
            document.body.classList.remove('cc-no-run');
        }
        if (entry.interpretation) {
            CC.state.latestInterpretation = entry.interpretation;
            document.getElementById('interpretationOutput').innerHTML =
                CC.renderMarkdown(entry.interpretation);
            if (CC.setInterpretationTitle) CC.setInterpretationTitle(entry.success === false);
        }
        CC.switchTab('generate');
        restoreSavedRun(entry);
        // A script this session did not generate is not refused outright.
        // /run gives it a safety re-check first, so a
        // restored script runs in a fresh session too (or is refused with a
        // reason, 403, or "could not check", 503).
        const same = entry.dataset_name && entry.dataset_name === CC.state.filename;
        const label = entry.dataset_label || entry.dataset_name || 'its dataset';
        CC.toast(same
            ? 'Past analysis restored. Click Run in sandbox to re-run it on the loaded dataset; '
              + 'a restored script gets a quick safety re-check before it runs.'
            : 'Past analysis restored. To re-run it, load ' + label + ' first. A restored '
              + 'script gets a quick safety re-check before it runs.',
            'info');
    }

    /** While the entry's workbook run still exists, bring back what the
     *  text-only history row cannot hold: the full output and the plots. An
     *  account row says so itself (`run_available`); a browser entry is
     *  checked here, and a run newer than the entry (an id reused after the
     *  workspace was wiped) is not this entry's run. */
    async function restoreSavedRun(entry) {
        if (!entry.run_id || !CC.workbook || !CC.renderSavedRun) return;
        if (entry.server && !entry.run_available) return;
        const got = await CC.workbook.fetchRun(entry.run_id);
        const run = got && got.run;
        if (!run) return;
        const created = Date.parse(run.created_at || '');
        if (entry.ts && !isNaN(created) && created > entry.ts + 5000) return;
        // Another restore or a new run may have replaced this one meanwhile.
        const current = CC.state.lastRun;
        if (current && (!current.restored || current.code !== (entry.code || ''))) return;
        if (CC.resultsBusy && CC.resultsBusy()) return;
        if (!run.interpretation && entry.interpretation) run.interpretation = entry.interpretation;
        CC.renderSavedRun(run);
        CC.state.lastRun = {
            output: run.output || '', plots: Array.isArray(run.plots) ? run.plots : [],
            success: !CC.savedRunFailed(run), code: entry.code || '', restored: true,
            run_id: run.run_id,
        };
        if (run.interpretation) CC.state.latestInterpretation = run.interpretation;
        CC.workbook.noteLiveRun(run.run_id);
    }

    // =====================================================================
    // Analysis catalog
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

    // One /method_prompt (a billed model call) at a time, so a double click
    // cannot send two (the second would capture the spinner as the label).
    let methodBusy = false;

    async function pickMethod(method, btn) {
        if (methodBusy) return;
        if (!CC.state.filename) {
            CC.toast('Upload a dataset first so STATlee can match the method to your columns.', 'error');
            return;
        }
        methodBusy = true;
        btn.disabled = true;
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
                CC.toast(data.rationale || 'Prompt drafted. Review it and hit Generate.', 'success');
            } else {
                CC.toast((data && data.error) || 'Could not draft a prompt for that method.', 'error');
            }
        } catch (e) {
            CC.toast('Network error.', 'error');
        } finally {
            btn.innerHTML = orig;
            btn.disabled = false;
            methodBusy = false;
        }
    }

    // =====================================================================
    // Issue reporting
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
    // Accounts + auth boot
    // =====================================================================
    /** The balance, shown only when billing is switched on (otherwise
     *  credits mean nothing). Called at sign-in with /check_auth's value and
     *  by CC.noteCredits whenever a reply carries `credits_remaining`. */
    CC.showCredits = function () {
        const credits = document.getElementById('accountCredits');
        if (!credits) return;
        const user = CC.state.user;
        const billing = window.CC_BOOT && window.CC_BOOT.billing;
        if (user && billing && typeof user.credits === 'number') {
            credits.textContent = user.credits + ' credits';
            credits.classList.remove('hidden');
        } else {
            credits.classList.add('hidden');
        }
    };

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
        CC.showCredits();
        if (CC.session) CC.session.armExpiry();
    }

    /** After /login or a signed-in /register (201). The server has moved this
     *  browser session's anonymous datasets, history, last run and approvals
     *  into the account, so the stored filename stays
     *  valid: keep the workspace instead of reloading it away.
     *
     *  `workspace` is the server's report: `{moved: [...], skipped:
     *  [...]}`, stored dataset names. A skipped name already existed in the
     *  account, whose copy wins; the user is told which ones. */
    async function afterSignIn(workspace) {
        let user = null;
        try {
            const res = await fetch('/check_auth');
            const data = await CC.readJSON(res);
            if (res.ok) user = data.user || null;
        } catch (e) { /* fall back to a reload below */ }
        if (!user) { window.location.reload(); return; }
        const modal = document.getElementById('accountModal');
        if (modal) delete modal.dataset.forced;   // REQUIRE_LOGIN's undismissable gate
        setAccountUI(user);
        CC.modal.close('accountModal');
        document.getElementById('accPassword').value = '';
        const signedIn = 'Signed in as ' + user.email + '.';
        const skipped = workspace && Array.isArray(workspace.skipped)
            ? workspace.skipped.filter(n => typeof n === 'string') : [];
        const current = CC.state.filename;
        if (skipped.length) {
            const others = skipped.filter(n => n !== current);
            const parts = [signedIn];
            if (current && skipped.indexOf(current) !== -1) {
                parts.push(CC.datasetLabel() + ' was not moved: your account already has a dataset '
                    + 'named ' + current + ', and the account copy is shown now. Upload the file '
                    + 'again to replace it.');
            }
            if (others.length) {
                parts.push((others.length === 1 ? 'One other dataset was' : others.length
                    + ' other datasets were') + ' not moved because the account already has the '
                    + 'same name: ' + others.join(', ') + '.');
            }
            CC.toast(parts.join(' '), 'info');
        } else if (current) {
            CC.toast(signedIn + ' ' + CC.datasetLabel() + ' moved into your account.', 'success');
        } else {
            CC.toast(signedIn, 'success');
        }
        if (current) {
            // Re-read the dataset under the account. When the account's own
            // copy of this name was kept, its columns and history replace the
            // anonymous ones on screen.
            if (CC.reloadDatasetState) await CC.reloadDatasetState();
            else CC.fetchDataPage(1);
        }
        // The workbook moved with the workspace (or the account's own runs
        // won): list what the account has now.
        if (CC.workbook) CC.workbook.refresh();
    }

    /** Status line in the account dialog. kind 'error' (red) or 'info'. */
    function accMessage(text, kind) {
        const el = document.getElementById('accError');
        if (!text) { el.classList.add('hidden'); el.textContent = ''; return; }
        el.textContent = text;
        el.classList.toggle('acc-info', kind === 'info');
        el.setAttribute('role', kind === 'info' ? 'status' : 'alert');
        el.classList.remove('hidden');
    }

    let accBusy = false;
    /** One account request at a time; every button in the form is disabled
     *  while it runs, so a double-click is not a double registration. */
    async function accAction(fn) {
        if (accBusy) return;
        accBusy = true;
        const ids = ['accLoginBtn', 'accRegisterBtn', 'accForgotBtn'];
        ids.forEach((id) => { const b = document.getElementById(id); if (b) b.disabled = true; });
        try {
            await fn();
        } catch (e) {
            accMessage('Network error: could not reach the server.', 'error');
        } finally {
            accBusy = false;
            ids.forEach((id) => { const b = document.getElementById(id); if (b) b.disabled = false; });
        }
    }

    function accCreds() {
        return {
            email: document.getElementById('accEmail').value.trim(),
            password: document.getElementById('accPassword').value,
        };
    }

    function doLogin() {
        return accAction(async () => {
            accMessage('');
            const { ok, status, data } = await CC.post('/login', accCreds());
            if (ok) { await afterSignIn(data && data.workspace); return; }
            accMessage(CC.errorMessage(status, data, 'Sign-in failed'), 'error');
        });
    }

    function doRegister() {
        return accAction(async () => {
            accMessage('');
            const { ok, status, data } = await CC.post('/register', accCreds());
            // Only 201 {status:'success'} means "signed in". A 202
            // (verification required / email failed) carries a message the
            // user must read, so no reload.
            if (ok && data && data.status === 'success') { await afterSignIn(data.workspace); return; }
            if (ok) {
                accMessage((data && data.message) ||
                    'Account created. Check your email to confirm it, then sign in.', 'info');
                return;
            }
            accMessage(CC.errorMessage(status, data, 'Registration failed'), 'error');
        });
    }

    function doForgot() {
        return accAction(async () => {
            accMessage('');
            const email = document.getElementById('accEmail').value.trim();
            if (!email) {
                accMessage('Enter your email above, then choose "Forgot password?".', 'error');
                document.getElementById('accEmail').focus();
                return;
            }
            const { ok, status, data } = await CC.post('/request_password_reset', { email });
            if (ok) {
                accMessage((data && data.message) ||
                    'If that email is registered, a reset link has been sent. Check your inbox.', 'info');
                return;
            }
            accMessage(CC.errorMessage(status, data, 'Could not request a reset'), 'error');
        });
    }

    async function doLogout() {
        try {
            await CC.post('/logout', {});
        } catch (e) {
            CC.toast('Network error: could not sign out.', 'error');
            return;
        }
        window.location.reload();
    }

    // =====================================================================
    // Anonymous session expiry
    // =====================================================================
    // Anonymous uploads are deleted by the TTL reaper (file_ttl from config).
    // Say when, before it happens, instead of letting the next click fail
    // with "Invalid filename".
    let expiryTimers = [];
    CC.session = {
        armExpiry() {
            expiryTimers.forEach(clearTimeout);
            expiryTimers = [];
            const el = document.getElementById('sessionExpiry');
            const ttl = Number(window.CC_BOOT && window.CC_BOOT.file_ttl) || 0;
            if (!el) return;
            if (CC.state.user || !CC.state.filename || !CC.state.uploadedAt || ttl <= 0) {
                el.classList.add('hidden');
                return;
            }
            const deadline = CC.state.uploadedAt + ttl * 1000;
            const at = new Date(deadline).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            el.textContent = 'Anonymous upload: deleted around ' + at;
            el.classList.remove('hidden');
            const warnIn = deadline - Date.now() - 10 * 60 * 1000;
            if (warnIn > 0) {
                expiryTimers.push(setTimeout(() => {
                    CC.toast('Your anonymous upload will be deleted in about 10 minutes. ' +
                        'Export the project or sign in to keep your work.', 'info');
                }, warnIn));
            }
            const goneIn = deadline - Date.now();
            if (goneIn > 0) {
                expiryTimers.push(setTimeout(() => {
                    el.textContent = 'Anonymous upload expired: upload the dataset again';
                }, goneIn));
            }
        },
    };

    async function checkAuth() {
        try {
            const res = await fetch('/check_auth');
            const data = await CC.readJSON(res);
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
        // Settles once the boot sign-in state is applied (checkAuth never
        // rejects). Anything that reads or seeds CC.state.user, the browser
        // tests included, can await it instead of racing the reply.
        CC.authReady = checkAuth();
        initLegacyPasswordOverlay();
        renderCatalog();

        document.getElementById('exportBtn').addEventListener('click', openExportDialog);
        document.getElementById('exportDownloadBtn').addEventListener('click', exportProject);
        document.getElementById('historyBtn').addEventListener('click', openHistory);
        const clearBtn = document.getElementById('historyClearBtn');
        if (clearBtn) clearBtn.addEventListener('click', clearHistory);
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
            // A real <form>, so Enter in either field signs in.
            const form = document.getElementById('accForm');
            if (form) {
                form.addEventListener('submit', (e) => { e.preventDefault(); doLogin(); });
            } else {
                document.getElementById('accLoginBtn').addEventListener('click', doLogin);
            }
            document.getElementById('accRegisterBtn').addEventListener('click', doRegister);
            const forgot = document.getElementById('accForgotBtn');
            if (forgot) forgot.addEventListener('click', doForgot);
            document.getElementById('accLogoutBtn').addEventListener('click', doLogout);
        } else {
            const accountBtn = document.getElementById('accountBtn');
            if (accountBtn) accountBtn.classList.add('hidden');
        }
    });
}());
