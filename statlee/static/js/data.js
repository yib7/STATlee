/* Dataset flows: upload (multi-format 5.1), codebook decision tree incl.
 * survey branch (5.13), classification + suggestions with reroll (4.5),
 * data table, conversational wrangling with changelog/undo/redo (5.16),
 * and reset (4.6). */
(function () {
    'use strict';

    let pendingFilename = null;
    let pdfMode = 'codebook';   // 'codebook' | 'survey' (5.13)
    let searchDebounce;

    // --- data table -----------------------------------------------------------
    CC.fetchDataPage = async function (page) {
        if (!CC.state.filename) return;
        const filters = {};
        document.querySelectorAll('.col-search-input').forEach(input => {
            if (input.value.trim() !== '') filters[input.dataset.col] = input.value.trim();
        });
        const { ok, data } = await CC.post('/data_page', {
            filename: CC.state.filename, page, per_page: 100, filters,
        });
        if (ok && data.status === 'success') {
            CC.state.currentPage = data.current_page;
            CC.state.totalPages = data.total_pages;
            renderTableBody(CC.state.headers, data.data);
            document.getElementById('paginationFooter').classList.remove('hidden');
            document.getElementById('pageInfo').innerText =
                `Page ${data.current_page} of ${data.total_pages} (${data.total_rows} matching rows)`;
            document.getElementById('prevPageBtn').disabled = data.current_page <= 1;
            document.getElementById('nextPageBtn').disabled = data.current_page >= data.total_pages;
        } else if (data && data.error) {
            CC.toast(data.error, 'error');
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
            input.addEventListener('keyup', () => {
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
                    const cell = CC.escapeHtml(row[h]);
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
    async function fetchCodebook(filename, quiet) {
        if (!quiet) CC.pipeline.set('classify', 'active');
        try {
            const { ok, data } = await CC.post('/classify_variables', { filename });
            if (ok && data.codebook) {
                CC.state.codebook = data.codebook;
                CC.renderCodebookUI();
                if (data.usage) CC.addUsage(data.usage);
                if (!quiet) CC.pipeline.set('classify', 'done', data.cached ? 'cached' : '');
                const generateBtn = document.getElementById('generateBtn');
                const promptInput = document.getElementById('promptInput');
                generateBtn.disabled = false;
                promptInput.placeholder = "e.g., Run a multiple regression using 'income' and 'education' to predict 'voting_behavior'. Generate a scatter plot of the residuals...";
            } else if (!quiet) {
                CC.pipeline.set('classify', 'failed', (data && data.error) || 'failed');
            }
        } catch (e) {
            if (!quiet) CC.pipeline.set('classify', 'failed', 'network error');
        }
    }
    CC.fetchCodebook = fetchCodebook;

    CC.fetchSuggestions = async function (filename, previous) {
        const container = document.getElementById('suggestionsContainer');
        const list = document.getElementById('suggestionsList');
        container.classList.remove('hidden');
        list.innerHTML = `<span class="text-xs text-indigo-700 dark:text-indigo-400 font-mono tracking-widest uppercase font-bold flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} Reading data structure...</span>`;
        if (!previous) CC.pipeline.set('suggest', 'active');
        try {
            const { ok, data } = await CC.post('/suggest', {
                filename,
                codebook: CC.state.codebook,
                pdf_mapping: CC.state.pdfMapping,
                previous: previous || [],
            });
            if (ok && data.suggestions) {
                CC.state.suggestions = data.suggestions;
                if (data.usage) CC.addUsage(data.usage);
                list.innerHTML = '';
                data.suggestions.forEach(suggestion => {
                    const btn = document.createElement('button');
                    btn.className = 'text-sm bg-white/80 hover:bg-white dark:bg-surface-2/50 dark:hover:bg-surface-2 text-indigo-900 dark:text-indigo-300 border border-slate-300 hover:border-indigo-400 dark:border-slate-700/50 dark:hover:border-indigo-500/50 py-3 px-4 rounded-xl text-left transition-all duration-300 shadow-sm hover:shadow-md flex items-center gap-3 group font-medium w-full';
                    btn.innerHTML = `<span class="opacity-50 group-hover:opacity-100 transition-opacity transform group-hover:scale-110 group-hover:text-amber-500" aria-hidden="true">✦</span> <span class="leading-relaxed">${CC.escapeHtml(suggestion)}</span>`;
                    btn.onclick = () => { document.getElementById('promptInput').value = suggestion; };
                    list.appendChild(btn);
                });
                if (!previous) CC.pipeline.set('suggest', 'done', data.cached ? 'cached' : '');
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
            CC.pipeline.set('suggest', 'skipped', 'disabled in settings');
        }
    }

    // --- upload flow ------------------------------------------------------------
    function initUpload() {
        document.getElementById('fileInput').addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);

            CC.pipeline.begin();
            CC.pipeline.set('load', 'active');

            const generateBtn = document.getElementById('generateBtn');
            const promptInput = document.getElementById('promptInput');
            generateBtn.disabled = true;
            promptInput.placeholder = 'Please wait, profiling dataset variables. This might take a sec..';

            // Reset prior dataset state so a fresh upload starts clean
            CC.state.pdfMapping = {};
            CC.state.codebook = {};
            CC.dockCodebook();
            document.getElementById('codebookContainer').classList.add('hidden');
            document.getElementById('codebookList').innerHTML = '';
            document.getElementById('changelogPanel').classList.add('hidden');

            try {
                const { ok, data } = await CC.postForm('/upload', formData);
                if (ok && data.filename) {
                    CC.state.filename = data.filename;
                    CC.state.chatHistory = [];
                    CC.state.converseHistory = [];
                    CC.state.headers = data.profile.headers;
                    if (data.labels && Object.keys(data.labels).length > 0) {
                        // Native .sav/.dta variable labels seed the codebook (5.1)
                        CC.state.pdfMapping = Object.assign({}, data.labels);
                        CC.toast(`Loaded ${Object.keys(data.labels).length} native variable labels.`, 'success');
                    }
                    CC.renderTableHeaders(CC.state.headers);
                    await CC.fetchDataPage(1);
                    renderChangelog(data.changelog);   // reveal the cleaning chat with v1
                    CC.pipeline.set('load', 'done');
                    pendingFilename = data.filename;
                    showCodebookStep('ask');
                } else {
                    CC.pipeline.set('load', 'failed', (data && data.error) || 'upload failed');
                    CC.toast((data && data.error) || 'Upload failed.', 'error');
                    e.target.value = '';
                }
            } catch (err) {
                CC.pipeline.set('load', 'failed', 'network error');
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
        document.getElementById('surveySkipBtn').addEventListener('click', async () => {
            hideCodebookPrompt();
            CC.pipeline.set('codebook_link', 'skipped');
            await continuePipeline();
        });

        document.getElementById('pdfInput').addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            hideCodebookPrompt();
            CC.pipeline.set('codebook_link', 'active');

            const fileURL = URL.createObjectURL(file);
            document.getElementById('pdfViewerFrame').src = fileURL;
            document.getElementById('tabPdf').style.display = 'flex';
            CC.switchTab('pdf');

            const formData = new FormData();
            formData.append('file', file);
            try {
                const { ok, data } = await CC.postForm('/upload_pdf', formData);
                if (ok && data.filename) {
                    const ext = await CC.post('/extract_pdf_codebook', {
                        filename: data.filename,
                        mode: pdfMode,
                        headers: CC.state.headers,
                    });
                    if (ext.ok && ext.data.status === 'success') {
                        CC.state.pdfMapping = Object.assign({}, CC.state.pdfMapping, ext.data.mapping);
                        if (ext.data.usage) CC.addUsage(ext.data.usage);
                        const n = Object.keys(ext.data.mapping).length;
                        const matched = Object.keys(ext.data.mapping)
                            .filter(k => CC.state.headers.some(h => h.toLowerCase() === k.toLowerCase())).length;
                        CC.pipeline.set('codebook_link', 'done',
                            pdfMode === 'survey' ? `survey: ${matched}/${CC.state.headers.length} columns matched` : `${n} definitions`);
                    } else {
                        CC.pipeline.set('codebook_link', 'failed', 'extraction failed');
                    }
                } else {
                    CC.pipeline.set('codebook_link', 'failed', (data && data.error) || 'upload failed');
                    CC.toast((data && data.error) || 'Documentation upload failed.', 'error');
                }
            } catch (err) {
                CC.pipeline.set('codebook_link', 'failed', 'network error');
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
        CC.modal.open('codebookPromptOverlay');
    }
    function hideCodebookPrompt() { CC.modal.close('codebookPromptOverlay'); }

    // --- wrangling + version control (5.16) -----------------------------------------
    function renderChangelog(changelog) {
        const panel = document.getElementById('changelogPanel');
        const list = document.getElementById('changelogList');
        if (!changelog) { panel.classList.add('hidden'); return; }
        panel.classList.remove('hidden');
        list.innerHTML = '';
        changelog.versions.forEach(v => {
            const active = v.v === changelog.active;
            if (v.summary) {
                // A user-driven edit: their words, then the applied result.
                const turn = document.createElement('div');
                turn.className = 'wrangle-turn' + (active ? ' active-version' : '');
                turn.innerHTML =
                    `<div class="wrangle-msg user"><span class="v-badge">v${v.v}</span>${CC.escapeHtml(v.instruction)}</div>` +
                    `<div class="wrangle-msg applied"><span aria-hidden="true">✓</span> ${CC.escapeHtml(v.summary)}</div>`;
                list.appendChild(turn);
            } else {
                // A milestone (original upload / reverted) — a centered note.
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
            await fetchCodebook(CC.state.filename, true);  // re-profile quietly
        }
    }

    function initWrangle() {
        const applyBtn = document.getElementById('wrangleApplyBtn');
        const input = document.getElementById('wrangleInput');

        function appendPending(instruction) {
            const list = document.getElementById('changelogList');
            const el = document.createElement('div');
            el.className = 'wrangle-turn pending';
            el.id = 'wranglePending';
            el.innerHTML =
                `<div class="wrangle-msg user">${CC.escapeHtml(instruction)}</div>` +
                `<div class="wrangle-msg applied">${CC.spinner('h-3 w-3')} Applying…</div>`;
            list.appendChild(el);
            list.scrollTop = list.scrollHeight;
        }
        function removePending() {
            const el = document.getElementById('wranglePending');
            if (el) el.remove();
        }

        async function applyWrangle() {
            const instruction = input.value.trim();
            if (!instruction || !CC.state.filename) return;
            applyBtn.disabled = true;
            applyBtn.innerHTML = CC.spinner('h-4 w-4');
            input.value = '';
            appendPending(instruction);              // optimistic chat echo
            try {
                const { ok, data } = await CC.post('/wrangle', {
                    filename: CC.state.filename, instruction,
                });
                if (ok && data.status === 'success') {
                    if (data.usage) CC.addUsage(data.usage);
                    CC.toast(`Applied: ${data.summary}`, 'success');
                    await afterVersionChange(data.profile, data.changelog);
                } else {
                    removePending();
                    input.value = instruction;       // restore for an easy retry
                    CC.toast((data && data.error) || 'Transformation failed.', 'error');
                }
            } catch (e) {
                removePending();
                input.value = instruction;
                CC.toast('Network error applying the transformation.', 'error');
            } finally {
                applyBtn.disabled = false;
                applyBtn.innerHTML = 'Apply';
            }
        }

        applyBtn.addEventListener('click', applyWrangle);
        input.addEventListener('keypress', (e) => { if (e.key === 'Enter') applyWrangle(); });

        document.getElementById('undoBtn').addEventListener('click', () => shiftVersion('undo'));
        document.getElementById('redoBtn').addEventListener('click', () => shiftVersion('redo'));
        document.getElementById('revertBtn').addEventListener('click', revertToOriginal);

        async function shiftVersion(direction) {
            const { ok, data } = await CC.post('/version_control', {
                filename: CC.state.filename, direction,
            });
            if (ok && data.status === 'success') {
                await afterVersionChange(data.profile, data.changelog);
            } else {
                CC.toast((data && data.error) || 'Could not change version.', 'error');
            }
        }

        async function revertToOriginal() {
            if (!CC.state.filename) return;
            if (!window.confirm('Revert the data to its original upload? You can Undo this afterwards.')) return;
            const { ok, data } = await CC.post('/revert_dataset', {
                filename: CC.state.filename,
            });
            if (ok && data.status === 'success') {
                CC.toast('Reverted to the original upload.', 'success');
                await afterVersionChange(data.profile, data.changelog);
            } else {
                CC.toast((data && data.error) || 'Could not revert the dataset.', 'error');
            }
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

    // --- suggestion reroll (4.5) -----------------------------------------------------------
    function initReroll() {
        document.getElementById('rerollBtn').addEventListener('click', () => {
            if (!CC.state.filename) return;
            CC.fetchSuggestions(CC.state.filename, CC.state.suggestions);
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        initUpload();
        initWrangle();
        initReset();
        initReroll();
    });
}());
