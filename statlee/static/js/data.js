/* Dataset flows: upload (multi-format 5.1), codebook decision tree incl.
 * survey branch (5.13), classification + suggestions with reroll (4.5),
 * data table, conversational wrangling with changelog/undo/redo (5.16),
 * and reset (4.6). */
(function () {
    'use strict';

    let pendingFilename = null;
    let pdfMode = 'codebook';   // 'codebook' | 'survey' (5.13)
    let searchDebounce;
    let pageSeq = 0;            // D4-23: only the newest /data_page reply renders
    let uploadBusy = false;
    let codebookURL = null;     // blob: URL of the attached document (revoked on replace)

    // --- data table -----------------------------------------------------------
    CC.fetchDataPage = async function (page) {
        if (!CC.state.filename) return;
        const filters = {};
        document.querySelectorAll('.col-search-input').forEach(input => {
            if (input.value.trim() !== '') filters[input.dataset.col] = input.value.trim();
        });
        const seq = ++pageSeq;
        const forFile = CC.state.filename;
        let reply;
        try {
            reply = await CC.post('/data_page', {
                filename: forFile, page, per_page: 100, filters,
            });
        } catch (e) {
            if (seq === pageSeq) CC.toast('Network error loading the data table.', 'error');
            return;
        }
        // A slower, older reply (a previous filter, or the previous dataset)
        // must not overwrite a newer one.
        if (seq !== pageSeq || forFile !== CC.state.filename) return;
        const { ok, data } = reply;
        if (ok && data.status === 'success') {
            const totalPages = Math.max(1, data.total_pages || 0);
            CC.state.currentPage = data.current_page;
            CC.state.totalPages = totalPages;
            renderTableBody(CC.state.headers, data.data);
            document.getElementById('paginationFooter').classList.remove('hidden');
            document.getElementById('pageInfo').innerText =
                `Page ${data.current_page} of ${totalPages} (${data.total_rows} matching rows)`;
            document.getElementById('prevPageBtn').disabled = data.current_page <= 1;
            document.getElementById('nextPageBtn').disabled = data.current_page >= totalPages;
        } else if (data && data.error) {
            CC.toast(CC.explainError(data.error), 'error');
        }
    };

    CC.changePage = function (delta) {
        const p = CC.state.currentPage + delta;
        if (p >= 1 && p <= CC.state.totalPages) CC.fetchDataPage(p);
    };
    window.changePage = CC.changePage;

    CC.renderTableHeaders = function (headers) {
        const thead = document.getElementById('dataTableHead');
        let html = '<tr>';
        headers.forEach(h => {
            const safeH = CC.escapeHtml(h);
            html += `
            <th class="px-4 py-3 whitespace-nowrap align-top transition-colors duration-300">
                <div class="mb-2 text-indigo-900 dark:text-indigo-300 font-bold text-sm">${safeH}</div>
                <input type="text" data-col="${safeH}" placeholder="Filter..." aria-label="Filter column ${safeH}" class="col-search-input w-full min-w-[120px] normal-case bg-white/90 dark:bg-black/40 border border-slate-300 dark:border-slate-700/50 rounded-lg px-2 py-1.5 text-slate-900 dark:text-slate-300 focus:outline-none focus:border-indigo-600 focus:ring-2 focus:ring-indigo-600 transition-all font-mono text-xs font-semibold">
            </th>`;
        });
        thead.innerHTML = html + '</tr>';
        document.querySelectorAll('.col-search-input').forEach(input => {
            // `input`, not `keyup`: Tab, arrows and Shift must not refetch.
            input.addEventListener('input', () => {
                clearTimeout(searchDebounce);
                searchDebounce = setTimeout(() => CC.fetchDataPage(1), 400);
            });
        });
    };

    function renderTableBody(headers, rows) {
        const tbody = document.getElementById('dataTableBody');
        let html = '';
        if (!rows || rows.length === 0) {
            html = `<tr><td colspan="${headers.length}" class="px-4 py-12 text-center text-slate-500 italic">No matching results found.</td></tr>`;
        } else {
            rows.forEach(row => {
                html += '<tr class="border-b border-slate-100/50 dark:border-slate-800/30 hover:bg-slate-50/50 dark:hover:bg-indigo-900/10 transition-colors">';
                headers.forEach(h => {
                    // D1-9: missing, NaN and +/-inf cells arrive as null. Mark
                    // them, so "missing" never looks like an empty string.
                    const value = row[h];
                    if (value === null || value === undefined) {
                        html += '<td class="cell-missing px-4 py-3 whitespace-nowrap" title="Missing value">NA</td>';
                        return;
                    }
                    const cell = CC.escapeHtml(value);
                    html += `<td class="px-4 py-3 whitespace-nowrap max-w-xs truncate" title="${cell}">${cell}</td>`;
                });
                html += '</tr>';
            });
        }
        tbody.innerHTML = html;
    }

    CC.viewColumn = function (colName) {
        CC.switchTab('data');
        const colIndex = CC.state.headers.indexOf(colName);
        if (colIndex === -1) return;
        const rows = document.getElementById('dataTable').rows;
        for (let i = 0; i < rows.length; i++) {
            for (let j = 0; j < rows[i].cells.length; j++) {
                rows[i].cells[j].classList.remove('bg-indigo-50', 'dark:bg-indigo-900/20');
            }
        }
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].cells[colIndex]) {
                rows[i].cells[colIndex].classList.add('bg-indigo-50', 'dark:bg-indigo-900/20');
                if (i === 0) {
                    setTimeout(() => {
                        const container = document.getElementById('tableScrollContainer');
                        const cell = rows[i].cells[colIndex];
                        if (container && cell) {
                            container.scrollTo({
                                left: cell.offsetLeft - (container.clientWidth / 2) + (cell.clientWidth / 2),
                                behavior: 'smooth',
                            });
                        }
                        const searchInput = cell.querySelector('input');
                        if (searchInput) searchInput.focus({ preventScroll: true });
                    }, 50);
                }
            }
        }
    };

    // --- post-upload pipeline (classification → suggestions) ---------------------
    /** Hand the composer back to the user once the dataset is profiled. Called
     *  on BOTH the success and failure paths: classification is optional context
     *  (generate() only requires CC.state.filename and merges an empty codebook
     *  fine), so a failed /classify_variables must not strand the run behind a
     *  Generate button that never re-enables. */
    function releaseComposer() {
        const generateBtn = document.getElementById('generateBtn');
        const promptInput = document.getElementById('promptInput');
        if (generateBtn) generateBtn.disabled = false;
        if (promptInput) {
            promptInput.placeholder = "e.g., Run a multiple regression using 'income' and 'education' to predict 'voting_behavior'. Generate a scatter plot of the residuals...";
        }
    }

    async function fetchCodebook(filename, quiet) {
        if (!quiet) CC.pipeline.set('classify', 'active');
        // A background re-classify (after a wrangle or a 409) is not awaited:
        // by the time it lands another dataset, or a re-upload of this one,
        // may be loaded. Its reply then describes data that is gone, and must
        // touch neither the new codebook nor the new dataset's composer.
        const forUpload = CC.state.uploadedAt;
        const current = () => filename === CC.state.filename && forUpload === CC.state.uploadedAt;
        try {
            const { ok, data } = await CC.post('/classify_variables', { filename });
            if (!current()) return;
            if (ok && data.codebook) {
                CC.state.codebook = data.codebook;
                CC.renderCodebookUI();
                if (data.usage) CC.addUsage(data.usage);
                // The classification is now in the server codebook (SP2),
                // under any edits the user made: show the merged result.
                if (CC.codebookEditor) CC.codebookEditor.refresh();
                if (!quiet) CC.pipeline.set('classify', 'done', data.cached ? 'cached' : '');
            } else if (!quiet) {
                CC.pipeline.set('classify', 'failed', (data && data.error) || 'failed');
            }
        } catch (e) {
            if (!quiet && current()) CC.pipeline.set('classify', 'failed', 'network error');
        } finally {
            if (current()) releaseComposer();
        }
    }

    /** /suggest accepts at most 10 `previous` ideas of 500 chars each (S1-B
     *  contract 9). The ideas on screen come first, so a reroll never repeats
     *  them; older ones fill the rest. */
    const PREVIOUS_MAX = 10;
    const PREVIOUS_CHARS = 500;
    function previousSuggestions(current) {
        const out = [];
        (current || []).concat(CC.state.seenSuggestions || []).forEach((s) => {
            if (typeof s !== 'string' || !s.trim()) return;
            const clipped = s.slice(0, PREVIOUS_CHARS);
            if (out.length < PREVIOUS_MAX && out.indexOf(clipped) === -1) out.push(clipped);
        });
        return out;
    }

    CC.fetchSuggestions = async function (filename, previous) {
        const container = document.getElementById('suggestionsContainer');
        const list = document.getElementById('suggestionsList');
        const nowBtn = document.getElementById('suggestNowBtn');
        if (nowBtn) nowBtn.classList.add('hidden');   // generating now: hide the prompt
        container.classList.remove('hidden');
        list.innerHTML = `<span class="text-xs text-indigo-700 dark:text-indigo-400 font-mono tracking-widest uppercase font-bold flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} Reading data structure...</span>`;
        if (!previous) CC.pipeline.set('suggest', 'active');
        try {
            const { ok, data } = await CC.post('/suggest', {
                filename,
                codebook: CC.state.codebook,
                pdf_mapping: CC.state.pdfMapping,
                previous: previous ? previousSuggestions(previous) : [],
            });
            // D4-21: the model's JSON shape is not guaranteed. Accept a list or
            // {suggestions: [...]}, keep strings only, at most three; anything
            // else is a failed step, not a TypeError mislabelled "network error".
            const raw = data && (Array.isArray(data.suggestions) ? data.suggestions
                : (data.suggestions && Array.isArray(data.suggestions.suggestions)
                    ? data.suggestions.suggestions : null));
            const suggestions = raw ? raw.filter(s => typeof s === 'string' && s.trim()).slice(0, 3) : [];
            if (ok && suggestions.length) {
                CC.state.suggestions = suggestions;
                CC.state.seenSuggestions = previousSuggestions(suggestions);
                if (data.usage) CC.addUsage(data.usage);
                list.innerHTML = '';
                suggestions.forEach(suggestion => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'text-sm bg-white/80 hover:bg-white dark:bg-surface-2/50 dark:hover:bg-surface-2 text-indigo-900 dark:text-indigo-300 border border-slate-300 hover:border-indigo-400 dark:border-slate-700/50 dark:hover:border-indigo-500/50 py-3 px-4 rounded-xl text-left transition-all duration-300 shadow-sm hover:shadow-md flex items-center gap-3 group font-medium w-full';
                    btn.innerHTML = `<svg class="w-4 h-4 shrink-0 opacity-50 group-hover:opacity-100 group-hover:text-amber-500 transition-all" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg> <span class="leading-relaxed">${CC.escapeHtml(suggestion)}</span>`;
                    btn.onclick = () => {
                        const prompt = document.getElementById('promptInput');
                        prompt.value = suggestion;
                        prompt.scrollIntoView({ block: 'center', behavior: 'smooth' });
                        prompt.focus({ preventScroll: true });
                    };
                    list.appendChild(btn);
                });
                if (!previous) CC.pipeline.set('suggest', 'done', data.cached ? 'cached' : '');
            } else if (ok) {
                list.innerHTML = '';
                if (!previous) CC.pipeline.set('suggest', 'failed', 'no usable suggestions');
            } else {
                list.innerHTML = '';
                if (!previous) CC.pipeline.set('suggest', 'failed', (data && data.error) || '');
            }
        } catch (e) {
            list.innerHTML = '';
            if (!previous) CC.pipeline.set('suggest', 'failed', 'network error');
        }
    };

    async function runPostUploadPipeline(filename) {
        await fetchCodebook(filename);
        // Auto-suggestions cost AI credits; skip when the user disabled them.
        if (!CC.prefs || CC.prefs.get('autosuggest', true)) {
            await CC.fetchSuggestions(filename);
        } else {
            // Disabled: don't auto-spend, but surface a one-click button so the
            // user can generate ideas on demand without re-uploading.
            CC.pipeline.set('suggest', 'skipped', 'disabled, use the button');
            const container = document.getElementById('suggestionsContainer');
            const list = document.getElementById('suggestionsList');
            const nowBtn = document.getElementById('suggestNowBtn');
            if (container) container.classList.remove('hidden');
            if (list) list.innerHTML = '';
            if (nowBtn) nowBtn.classList.remove('hidden');
        }
    }

    // Opens the dataset picker from outside the sidebar (the Data tab's empty
    // state), so the workspace is not a dead end when the sidebar is collapsed.
    // Routed through boot.js's data-action contract; CSP forbids inline onclick.
    CC.pickDataset = function () {
        const input = document.getElementById('fileInput');
        if (input) input.click();
    };
    window.pickDataset = CC.pickDataset;

    // --- upload flow ------------------------------------------------------------
    /** Clear the previous dataset's sidebar state. Runs only once the NEW
     *  upload has been accepted (D4-2): a rejected file must leave the dataset
     *  the user is working on untouched. */
    function resetDatasetChrome() {
        CC.state.pdfMapping = {};
        CC.state.codebook = {};
        CC.state.suggestions = [];
        CC.state.seenSuggestions = [];
        CC.dockCodebook();
        document.getElementById('codebookContainer').classList.add('hidden');
        document.getElementById('codebookList').innerHTML = '';
        if (CC.dockWrangle) CC.dockWrangle();   // dock any floating cleaning panel before hiding
        document.getElementById('changelogPanel').classList.add('hidden');
        resetCodebookViewer();   // clear any prior codebook doc, back to empty state
        if (CC.codebookEditor) CC.codebookEditor.show('variables');
    }

    /** The sidebar's "Loaded <name>" line (D1-1) plus upload warnings, e.g.
     *  "This workbook has 3 sheets. Imported 'Data'..." (D1-13). Quiet, in
     *  place, and kept until the next upload: nothing to dismiss. */
    function showDatasetInfo(data) {
        const box = document.getElementById('datasetInfo');
        if (!box) return;
        document.getElementById('datasetName').textContent = CC.datasetLabel();
        const stored = document.getElementById('datasetStored');
        if (stored) {
            const differs = CC.state.originalName && CC.state.originalName !== CC.state.filename;
            stored.textContent = differs ? `Stored as ${CC.state.filename}: scripts read this name.` : '';
            stored.classList.toggle('hidden', !differs);
        }
        const list = document.getElementById('datasetWarnings');
        if (list) {
            const warnings = Array.isArray(data.warnings)
                ? data.warnings.filter(w => typeof w === 'string' && w.trim()) : [];
            list.innerHTML = '';
            warnings.forEach((w) => {
                const li = document.createElement('li');
                li.textContent = w;
                list.appendChild(li);
            });
            list.classList.toggle('hidden', warnings.length === 0);
        }
        box.classList.remove('hidden');
    }

    /** Is there analysis work tied to the current dataset that a new upload
     *  would discard? (D4-3) */
    function hasAnalysisWork() {
        return !!(CC.getCode && CC.getCode().trim()) || !!CC.state.lastRun ||
            CC.state.converseHistory.length > 0 || !!(CC.state.report && CC.state.report.trim());
    }

    function initUpload() {
        document.getElementById('fileInput').addEventListener('change', async (e) => {
            const input = e.target;
            const file = input.files[0];
            if (!file) return;
            if (uploadBusy) { input.value = ''; return; }
            if (CC.state.filename && hasAnalysisWork() && !window.confirm(
                'Load a new dataset? The current script, results, conversation and report draft '
                + 'belong to the current dataset and will be cleared.')) {
                input.value = '';
                return;
            }
            uploadBusy = true;
            const formData = new FormData();
            formData.append('file', file);

            const previous = CC.state.filename;
            const previousLabel = CC.datasetLabel();
            const box = document.getElementById('pipelineChecklist');
            const statusEl = document.getElementById('pipelineStatus');
            const snapshot = { html: box ? box.innerHTML : '', status: statusEl ? statusEl.textContent : '' };
            function restorePipeline() {
                if (box) box.innerHTML = snapshot.html;
                if (statusEl) statusEl.textContent = snapshot.status;
            }

            CC.pipeline.begin();
            CC.pipeline.set('load', 'active');

            const generateBtn = document.getElementById('generateBtn');
            const promptInput = document.getElementById('promptInput');
            const placeholder = promptInput.placeholder;
            generateBtn.disabled = true;
            promptInput.placeholder = 'Please wait, profiling dataset variables. This might take a sec..';

            function failed(message) {
                if (previous) {
                    // Keep working on the dataset that is still loaded.
                    restorePipeline();
                    releaseComposer();
                    CC.toast(`${message} Still using ${previousLabel}.`, 'error');
                } else {
                    CC.pipeline.set('load', 'failed', message);
                    promptInput.placeholder = placeholder;
                    CC.toast(message, 'error');
                }
            }

            try {
                const { ok, status, data } = await CC.postForm('/upload', formData);
                if (ok && data.filename) {
                    resetDatasetChrome();
                    if (CC.resetAnalysisState) CC.resetAnalysisState();
                    // D1-1: the stored name may differ from the picked file
                    // (`<ascii>_<8hex>.csv`, `upload_<8hex>.csv`). Every
                    // request uses the stored one; the user sees their own.
                    CC.state.filename = data.filename;
                    CC.state.originalName = data.original_name || file.name;
                    const label = CC.datasetLabel();
                    showDatasetInfo(data);
                    if (previous && previous === data.filename) {
                        // D1-14: same stored name = the server replaced the
                        // dataset and restarted its cleaning history at v1.
                        CC.toast(`Replaced ${label}. Its cleaning history starts again at v1.`, 'info');
                    }
                    CC.state.uploadedAt = Date.now();
                    if (CC.session) CC.session.armExpiry();
                    // Real data exists now: retire the dropzone-era empty states
                    // (Data tab skeleton, composer "upload to enable" hint).
                    document.body.classList.remove('cc-no-data');
                    CC.state.chatHistory = [];
                    CC.state.converseHistory = [];
                    CC.state.headers = data.profile.headers;
                    if (data.labels && Object.keys(data.labels).length > 0) {
                        // Native .sav/.dta variable labels seed the codebook (5.1)
                        CC.state.pdfMapping = Object.assign({}, data.labels);
                        // D1-13: value labels are merged into the column label,
                        // e.g. "Sex (1=Male, 2=Female)".
                        const n = Object.keys(data.labels).length;
                        const coded = Object.values(data.labels)
                            .filter(v => String(v).indexOf('=') !== -1).length;
                        CC.toast(`Loaded ${n} native variable labels` +
                            (coded ? `, ${coded} with value labels.` : '.'), 'success');
                    }
                    CC.renderTableHeaders(CC.state.headers);
                    // The server codebook of the new dataset: native .sav/.dta
                    // labels now, and earlier edits when identical content
                    // was uploaded before (SP2).
                    if (CC.codebookEditor) CC.codebookEditor.refresh();
                    if (data.codebook_restored) {
                        CC.toast('The codebook from your earlier upload of this file is back.', 'info');
                    }
                    CC.pipeline.set('load', 'done', label);
                    // The table preview is its own request: a /data_page hiccup
                    // must not mark an accepted upload as failed (D4-23).
                    CC.fetchDataPage(1);
                    renderChangelog(data.changelog);   // reveal the cleaning chat with v1
                    pendingFilename = data.filename;
                    showCodebookStep('ask');
                    CC.announce(`Dataset ${label} loaded.`);
                } else {
                    // 422: empty, header-only or ragged file, or a profile
                    // failure; 413: too large. The server says which; name
                    // the file it is about.
                    const reason = (data && data.error) || `Upload failed (HTTP ${status}).`;
                    failed(`Could not load ${file.name}. ${reason}`);
                }
            } catch (err) {
                failed('Network error: the upload did not reach the server.');
            } finally {
                uploadBusy = false;
                input.value = '';   // D4-18: re-picking the same file must fire `change` again
            }
        });

        // Codebook decision tree (5.13): ask → [upload codebook] / [no →
        // ask survey] → [upload survey] / [skip]
        document.getElementById('codebookYesBtn').addEventListener('click', () => {
            pdfMode = 'codebook';
            document.getElementById('pdfInput').click();
        });
        document.getElementById('codebookNoBtn').addEventListener('click', () => {
            showCodebookStep('survey');
        });
        document.getElementById('surveyYesBtn').addEventListener('click', () => {
            pdfMode = 'survey';
            document.getElementById('pdfInput').click();
        });
        async function skipCodebook() {
            CC.pipeline.set('codebook_link', 'skipped');
            await continuePipeline();
        }
        document.getElementById('surveySkipBtn').addEventListener('click', async () => {
            hideCodebookPrompt();
            await skipCodebook();
        });
        // D4-1: Escape / closing the prompt is the usual way to say "no
        // thanks". It must mean "skip", or classification never starts and
        // Generate stays disabled forever.
        CC.modal.onDismiss('codebookPromptOverlay', skipCodebook);

        document.getElementById('pdfInput').addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            hideCodebookPrompt();
            CC.pipeline.set('codebook_link', 'active');

            const formData = new FormData();
            formData.append('file', file);
            try {
                const { ok, status, data } = await CC.postForm('/upload_pdf', formData);
                if (ok && data.filename) {
                    // D4-30: show the document only once the server accepted it,
                    // so a rejected PDF never reads as "attached".
                    await showCodebookDocument(file, data.filename);   // swap empty state -> the document
                    CC.switchTab('pdf');
                    const ext = await CC.post('/extract_pdf_codebook', {
                        filename: data.filename,
                        dataset: CC.state.filename,   // seeds the server codebook (SP2)
                        mode: pdfMode,
                        headers: CC.state.headers,
                    });
                    if (ext.ok && ext.data.status === 'success') {
                        CC.state.pdfMapping = Object.assign({}, CC.state.pdfMapping, ext.data.mapping);
                        if (ext.data.usage) CC.addUsage(ext.data.usage);
                        CC.renderCodebookUI();   // surface new descriptions on hover (no-op if not yet classified)
                        if (CC.codebookEditor) CC.codebookEditor.refresh();   // labels from the document (SP2)
                        const n = Object.keys(ext.data.mapping).length;
                        const matched = Object.keys(ext.data.mapping)
                            .filter(k => CC.state.headers.some(h => h.toLowerCase() === k.toLowerCase())).length;
                        CC.pipeline.set('codebook_link', 'done',
                            pdfMode === 'survey' ? `survey: ${matched}/${CC.state.headers.length} columns matched` : `${n} definitions`);
                    } else {
                        CC.pipeline.set('codebook_link', 'failed', 'extraction failed');
                        CC.toast(CC.errorMessage(ext.status, ext.data,
                            'Could not read variable definitions from that document') +
                            ' The analysis continues without them.', 'error');
                    }
                } else {
                    const msg = CC.errorMessage(status, data, 'Documentation upload failed');
                    CC.pipeline.set('codebook_link', 'failed', (data && data.error) || 'upload failed');
                    CC.toast(msg, 'error');
                }
            } catch (err) {
                CC.pipeline.set('codebook_link', 'failed', 'network error');
                CC.toast('Network error uploading the document.', 'error');
            }
            e.target.value = '';
            await continuePipeline();
        });
    }

    async function continuePipeline() {
        if (pendingFilename) {
            const fn = pendingFilename;
            pendingFilename = null;
            await runPostUploadPipeline(fn);
        }
    }

    function showCodebookStep(step) {
        document.getElementById('codebookStepAsk').classList.toggle('hidden', step !== 'ask');
        document.getElementById('codebookStepSurvey').classList.toggle('hidden', step !== 'survey');
        const num = document.getElementById('codebookStepNum');
        if (num) num.textContent = step === 'survey' ? '2' : '1';
        // Only the visible step's heading may label the dialog.
        const overlay = document.getElementById('codebookPromptOverlay');
        if (overlay) {
            overlay.setAttribute('aria-labelledby',
                step === 'survey' ? 'codebookSurveyTitle' : 'codebookPromptTitle');
        }
        CC.modal.open('codebookPromptOverlay');
        if (step === 'survey') {
            const yes = document.getElementById('surveyYesBtn');
            if (yes) setTimeout(() => yes.focus(), 40);
        }
    }
    function hideCodebookPrompt() { CC.modal.close('codebookPromptOverlay'); }

    // --- wrangling + version control (5.16) -----------------------------------------
    function renderChangelog(changelog) {
        const panel = document.getElementById('changelogPanel');
        const list = document.getElementById('changelogList');
        if (!changelog) { panel.classList.add('hidden'); return; }
        // While the cleaning tool floats, its body is in the float: the
        // sidebar panel stays hidden until it is docked (dockWrangle).
        if (!document.getElementById('wrangleFloat')) panel.classList.remove('hidden');
        list.innerHTML = '';
        changelog.versions.forEach(v => {
            const active = v.v === changelog.active;
            if (v.summary) {
                // A user-driven edit: their words, then the applied result.
                const turn = document.createElement('div');
                turn.className = 'wrangle-turn' + (active ? ' active-version' : '');
                turn.innerHTML =
                    `<div class="wrangle-msg user"><span class="v-badge">v${v.v}</span>${CC.escapeHtml(v.instruction)}</div>` +
                    `<div class="wrangle-msg applied"><svg class="w-3 h-3 shrink-0" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"></path></svg> ${CC.escapeHtml(v.summary)}</div>`;
                list.appendChild(turn);
            } else {
                // A milestone (original upload / reverted): a centered note.
                const note = document.createElement('div');
                note.className = 'wrangle-note' + (active ? ' active-version' : '');
                note.innerHTML = `<span class="v-badge">v${v.v}</span>${CC.escapeHtml(v.instruction)}`;
                list.appendChild(note);
            }
        });
        list.scrollTop = list.scrollHeight;        // newest at the bottom, chat-style
        document.getElementById('undoBtn').disabled = !changelog.can_undo;
        document.getElementById('redoBtn').disabled = !changelog.can_redo;
        const revertBtn = document.getElementById('revertBtn');
        if (revertBtn) revertBtn.disabled = (changelog.versions.length <= 1);
    }

    async function afterVersionChange(profile, changelog) {
        renderChangelog(changelog);
        if (profile) {
            CC.state.headers = profile.headers;
            CC.renderTableHeaders(CC.state.headers);
            await CC.fetchDataPage(1);
            // The new version's columns: added ones get a default codebook
            // row now, the re-classify below fills them in (SP2).
            if (CC.codebookEditor) CC.codebookEditor.refresh();
            // Re-classify in the BACKGROUND, not awaited: the edit itself is
            // already done and shown above. Awaiting this slow LLM re-profile
            // used to hold the Apply button's spinner spinning for seconds after
            // a successful edit. Fire-and-forget; it swallows its own errors and
            // refreshes the codebook chips when it lands.
            fetchCodebook(CC.state.filename, true);
        }
    }

    /** Adopt `cols` as the table's columns when they differ from the
     *  current ones, then re-classify in the background. */
    function adoptHeaders(filename, cols) {
        const changed = cols.length !== CC.state.headers.length
            || cols.some((h, i) => h !== CC.state.headers[i]);
        if (!changed) return;
        CC.state.headers = cols;
        CC.renderTableHeaders(cols);
        fetchCodebook(filename, true);   // background, like afterVersionChange
    }

    /** Re-read the dataset after a 409 (L1), or after sign-in kept the
     *  account's copy of this name: the cleaning history and the active
     *  version's columns from /dataset_versions (S5: it returns `headers` in
     *  the frame's own order), then the table. Returns the fresh changelog,
     *  or null. */
    async function reloadDatasetState() {
        const filename = CC.state.filename;
        if (!filename) return null;
        let changelog = null;
        try {
            const { ok, data } = await CC.post('/dataset_versions', { filename });
            if (ok && data.changelog && filename === CC.state.filename) {
                changelog = data.changelog;
                renderChangelog(changelog);
            }
            if (ok && Array.isArray(data.headers) && filename === CC.state.filename) {
                adoptHeaders(filename, data.headers);
            }
        } catch (e) { /* the caller's toast still tells the user to reload */ }
        await CC.fetchDataPage(1);
        if (CC.codebookEditor) CC.codebookEditor.refresh();
        return changelog;
    }
    CC.reloadDatasetState = reloadDatasetState;

    function initWrangle() {
        const applyBtn = document.getElementById('wrangleApplyBtn');
        const input = document.getElementById('wrangleInput');

        // D4-6: one dataset-version change at a time. A second /wrangle (or an
        // undo) fired while one is in flight would branch two versions off the
        // same parent.
        let versionBusy = false;
        const versionButtons = ['undoBtn', 'redoBtn', 'revertBtn'].map(id => document.getElementById(id));
        let savedDisabled = [];
        function setVersionBusy(on) {
            versionBusy = on;
            applyBtn.disabled = on;
            applyBtn.setAttribute('aria-busy', on ? 'true' : 'false');
            if (on) {
                savedDisabled = versionButtons.map(b => !!(b && b.disabled));
                versionButtons.forEach(b => { if (b) b.disabled = true; });
            }
        }
        // No fresh changelog (the change failed): put undo/redo/revert back
        // exactly as they were before the attempt.
        function restoreVersionButtons() {
            versionButtons.forEach((b, i) => { if (b) b.disabled = !!savedDisabled[i]; });
        }

        function appendPending(instruction) {
            const list = document.getElementById('changelogList');
            const el = document.createElement('div');
            el.className = 'wrangle-turn pending';
            el.innerHTML =
                `<div class="wrangle-msg user">${CC.escapeHtml(instruction)}</div>` +
                `<div class="wrangle-msg applied">${CC.spinner('h-3 w-3')} Applying…</div>`;
            list.appendChild(el);
            list.scrollTop = list.scrollHeight;
            return el;
        }

        async function applyWrangle() {
            const instruction = input.value.trim();
            if (versionBusy || !instruction || !CC.state.filename) return;
            setVersionBusy(true);
            applyBtn.innerHTML = CC.spinner('h-4 w-4');
            input.value = '';
            const pending = appendPending(instruction);   // optimistic chat echo
            let changelog = null;
            const forUpload = CC.state.uploadedAt;
            try {
                const { ok, status, data } = await CC.post('/wrangle', {
                    filename: CC.state.filename, instruction,
                });
                if (forUpload !== CC.state.uploadedAt) {
                    // A new dataset (or a re-upload of this one) was loaded
                    // while the edit ran: its reply describes the old data.
                    return;
                }
                if (ok && data.status === 'success') {
                    if (data.usage) CC.addUsage(data.usage);
                    CC.toast(`Applied: ${data.summary}`, 'success');
                    changelog = data.changelog;
                    await afterVersionChange(data.profile, data.changelog);
                } else if (status === 409) {
                    // L1: another tab's wrangle / undo / revert moved the
                    // active version while this one ran; nothing was saved.
                    // Show the data as it now is before the user retries.
                    pending.remove();
                    if (!input.value) input.value = instruction;
                    changelog = await reloadDatasetState();
                    CC.toast('Your edit was not applied: the dataset changed in the meantime '
                        + '(another tab or window). The data and the cleaning history have been '
                        + 'reloaded. Check them, then press Apply to try your edit again.', 'error');
                } else {
                    pending.remove();
                    if (!input.value) input.value = instruction;   // restore for an easy retry
                    let msg = CC.errorMessage(status, data, 'Transformation failed');
                    if (status === 502) {
                        // A malformed plan from the model: worth a rephrase.
                        msg += ' The model returned an unusable plan; try rephrasing the instruction.';
                    }
                    CC.toast(msg, 'error');
                }
            } catch (e) {
                pending.remove();
                if (!input.value) input.value = instruction;
                CC.toast('Network error applying the transformation.', 'error');
            } finally {
                pending.remove();
                setVersionBusy(false);
                applyBtn.innerHTML = 'Apply';
                if (forUpload !== CC.state.uploadedAt) {
                    // The new upload already rendered its own v1 changelog.
                } else if (changelog) renderChangelog(changelog);     // re-derive undo/redo state
                else restoreVersionButtons();
            }
        }
        applyBtn.addEventListener('click', applyWrangle);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); applyWrangle(); }
        });

        document.getElementById('undoBtn').addEventListener('click', () => shiftVersion('undo'));
        document.getElementById('redoBtn').addEventListener('click', () => shiftVersion('redo'));
        document.getElementById('revertBtn').addEventListener('click', revertToOriginal);

        async function versionCall(url, body, okToast, failMsg) {
            if (versionBusy || !CC.state.filename) return;
            setVersionBusy(true);
            let changelog = null;
            try {
                const { ok, status, data } = await CC.post(url, body);
                if (ok && data.status === 'success') {
                    if (okToast) CC.toast(okToast, 'success');
                    changelog = data.changelog;
                    await afterVersionChange(data.profile, data.changelog);
                } else {
                    CC.toast(CC.errorMessage(status, data, failMsg), 'error');
                }
            } catch (e) {
                CC.toast('Network error: ' + failMsg.toLowerCase() + '.', 'error');
            } finally {
                setVersionBusy(false);
                if (changelog) renderChangelog(changelog);
                else restoreVersionButtons();
            }
        }

        function shiftVersion(direction) {
            return versionCall('/version_control', { filename: CC.state.filename, direction },
                null, 'Could not change version');
        }

        function revertToOriginal() {
            if (versionBusy || !CC.state.filename) return;
            if (!window.confirm('Revert the data to its original upload? You can Undo this afterwards.')) return;
            return versionCall('/revert_dataset', { filename: CC.state.filename },
                'Reverted to the original upload.', 'Could not revert the dataset');
        }
    }

    // --- reset (4.6) --------------------------------------------------------------------
    function initReset() {
        document.getElementById('resetBtn').addEventListener('click', async () => {
            if (!window.confirm('Start a new analysis? This clears the current dataset, generated code, chats, and results.')) return;
            try { await CC.post('/reset', {}); } catch (e) { /* still reload */ }
            window.location.reload();
        });
    }

    // --- codebook viewer tab (empty state <-> loaded document) --------------------------
    /** Decode a local text file like the server does (D1-8/D1-10): UTF-8
     *  first, then Windows-1252, so a cp1252 codebook does not preview as
     *  replacement characters. */
    async function readText(file) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes).replace(/^﻿/, ''); }
        catch (e) { return new TextDecoder('windows-1252').decode(bytes); }
    }

    /** Show the attached document. The server keeps a .txt codebook as text
     *  (S1-B contract 6), so it is decided by the STORED name: a PDF goes in
     *  the frame as a typed blob, a TXT is rendered as plain text. A text
     *  blob in the frame would be sniffed and run at the app's origin. */
    async function showCodebookDocument(file, storedName) {
        const frame = document.getElementById('pdfViewerFrame');
        const text = document.getElementById('codebookTextView');
        const empty = document.getElementById('codebookEmptyState');
        const replace = document.getElementById('codebookReplaceBtn');
        if (codebookURL) { URL.revokeObjectURL(codebookURL); codebookURL = null; }
        const isText = /\.txt$/i.test(storedName || file.name || '');
        if (isText) {
            if (frame) { frame.src = 'about:blank'; frame.classList.add('hidden'); }
            if (text) {
                try { text.textContent = await readText(file); }
                catch (e) { text.textContent = 'Could not preview this text file.'; }
                text.classList.remove('hidden');
            }
        } else {
            codebookURL = URL.createObjectURL(new Blob([file], { type: 'application/pdf' }));
            if (text) { text.textContent = ''; text.classList.add('hidden'); }
            if (frame) { frame.src = codebookURL; frame.classList.remove('hidden'); }
        }
        // The user's own document name (upload_pdf `original_name`), not
        // the stored ASCII one.
        const label = 'Uploaded codebook: ' + (file.name || storedName || 'document');
        if (frame) frame.title = label;
        if (text) text.setAttribute('aria-label', label);
        if (empty) empty.classList.add('hidden');
        if (replace) replace.classList.remove('hidden');
        if (CC.codebookEditor) CC.codebookEditor.show('document');
    }
    function resetCodebookViewer() {
        const frame = document.getElementById('pdfViewerFrame');
        const text = document.getElementById('codebookTextView');
        const empty = document.getElementById('codebookEmptyState');
        const replace = document.getElementById('codebookReplaceBtn');
        if (frame) { frame.src = 'about:blank'; frame.classList.add('hidden'); }
        if (text) { text.textContent = ''; text.classList.add('hidden'); }
        if (empty) empty.classList.remove('hidden');
        if (replace) replace.classList.add('hidden');
        if (codebookURL) { URL.revokeObjectURL(codebookURL); codebookURL = null; }
    }
    // The Codebook tab is always reachable now. Its empty-state buttons let the
    // user add a codebook (or a survey to generate descriptions) at any time
    // WITHOUT re-uploading the dataset: they reuse the existing pdfInput flow.
    function initCodebookTab() {
        const uploadBtn = document.getElementById('codebookTabUploadBtn');
        const surveyBtn = document.getElementById('codebookTabSurveyBtn');
        function pickCodebook(mode) {
            if (!CC.state.filename) {
                CC.toast('Upload a dataset first, then add a codebook here.', 'info');
                return;
            }
            pdfMode = mode;
            document.getElementById('pdfInput').click();
        }
        if (uploadBtn) uploadBtn.addEventListener('click', () => pickCodebook('codebook'));
        if (surveyBtn) surveyBtn.addEventListener('click', () => pickCodebook('survey'));
        // D4-30: an attached document can be swapped without re-uploading data.
        const replaceBtn = document.getElementById('codebookReplaceBtn');
        if (replaceBtn) replaceBtn.addEventListener('click', () => pickCodebook('codebook'));
    }

    // --- data-viewer zoom -----------------------------------------------------------------
    let dataZoom = 1;
    function applyDataZoom() {
        const table = document.getElementById('dataTable');
        if (table) table.style.zoom = dataZoom;
        const reset = document.getElementById('dataZoomReset');
        if (reset) reset.textContent = Math.round(dataZoom * 100) + '%';
    }
    function setDataZoom(z) {
        dataZoom = Math.min(1.8, Math.max(0.6, Math.round(z * 10) / 10));
        applyDataZoom();
    }
    function initDataZoom() {
        const inB = document.getElementById('dataZoomIn');
        const outB = document.getElementById('dataZoomOut');
        const resetB = document.getElementById('dataZoomReset');
        if (inB) inB.addEventListener('click', () => setDataZoom(dataZoom + 0.1));
        if (outB) outB.addEventListener('click', () => setDataZoom(dataZoom - 0.1));
        if (resetB) resetB.addEventListener('click', () => setDataZoom(1));
        const scroll = document.getElementById('tableScrollContainer');
        if (scroll) {
            scroll.addEventListener('wheel', (e) => {
                if (!e.ctrlKey) return;        // Ctrl+wheel zooms; plain wheel scrolls
                e.preventDefault();
                setDataZoom(dataZoom + (e.deltaY < 0 ? 0.1 : -0.1));
            }, { passive: false });
        }
        applyDataZoom();
    }

    // --- suggestion reroll (4.5) -----------------------------------------------------------
    // A reroll skips the server cache by design, so every click is a billed
    // model call: one user-asked /suggest at a time, the button disabled
    // while it runs (a double click used to send two).
    let manualSuggestBusy = false;
    async function manualSuggest(btn, previous) {
        if (manualSuggestBusy || !CC.state.filename) return;
        manualSuggestBusy = true;
        btn.disabled = true;
        try {
            await CC.fetchSuggestions(CC.state.filename, previous);
        } finally {
            manualSuggestBusy = false;
            btn.disabled = false;
        }
    }

    function initReroll() {
        const btn = document.getElementById('rerollBtn');
        btn.addEventListener('click', () => manualSuggest(btn, CC.state.suggestions));
    }

    // On-demand suggestions when auto-suggest is off (no re-upload needed).
    function initSuggestNow() {
        const btn = document.getElementById('suggestNowBtn');
        if (!btn) return;
        btn.addEventListener('click', () => manualSuggest(btn));
    }

    document.addEventListener('DOMContentLoaded', () => {
        initUpload();
        initWrangle();
        initReset();
        initReroll();
        initSuggestNow();
        initDataZoom();
        initCodebookTab();
    });
}());
