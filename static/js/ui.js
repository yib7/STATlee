/* UI chrome: theme, modals, pipeline checklist (4.4), tab + split-pane
 * workspace (6.1), sidebar resizer, codebook rendering + pop-out (6.2). */
(function () {
    'use strict';

    // --- theme -------------------------------------------------------------
    CC.toggleTheme = function () {
        document.documentElement.classList.toggle('dark');
        localStorage.setItem('theme',
            document.documentElement.classList.contains('dark') ? 'dark' : 'light');
    };

    // --- modal helpers --------------------------------------------------------
    CC.modal = {
        open(id) {
            const el = document.getElementById(id);
            if (el) { el.classList.remove('hidden'); }
        },
        close(id) {
            const el = document.getElementById(id);
            if (el) { el.classList.add('hidden'); }
        },
    };
    document.addEventListener('click', (e) => {
        const closer = e.target.closest('[data-close-modal]');
        if (closer) CC.modal.close(closer.getAttribute('data-close-modal'));
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.querySelectorAll('.cc-modal:not(.hidden)').forEach(m => {
                if (!m.dataset.forced) m.classList.add('hidden');
            });
        }
    });

    // --- pipeline status checklist (4.4) ---------------------------------------
    const PIPELINE_STEPS = [
        { id: 'load', label: 'Data loaded' },
        { id: 'codebook_link', label: 'Codebook linked' },
        { id: 'classify', label: 'Variables classified' },
        { id: 'suggest', label: 'Suggestions ready' },
    ];

    CC.pipeline = {
        begin() {
            const box = document.getElementById('pipelineChecklist');
            if (!box) return;
            box.classList.remove('hidden');
            box.innerHTML = PIPELINE_STEPS.map(s =>
                `<div class="pipeline-step" data-step="${s.id}" data-state="pending">` +
                `<span class="step-dot" aria-hidden="true"></span>` +
                `<span>${s.label}</span><span class="step-note font-normal normal-case tracking-normal opacity-80"></span></div>`
            ).join('');
        },
        set(stepId, state, note) {
            const el = document.querySelector(`#pipelineChecklist [data-step="${stepId}"]`);
            if (!el) return;
            el.dataset.state = state;
            const dot = el.querySelector('.step-dot');
            const CHECK = '<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 13l4 4L19 7"/></svg>';
            const CROSS = '<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
            if (state === 'done') dot.innerHTML = CHECK;
            else if (state === 'failed') dot.innerHTML = CROSS;
            else dot.innerHTML = '';
            el.querySelector('.step-note').textContent = note ? ` — ${note}` : '';
        },
    };

    // --- tabs + split panes (6.1) ------------------------------------------------
    const VIEWS = ['Generate', 'Data', 'Results', 'Converse', 'Pdf'];
    let activeA = 'generate';
    let paneBView = null;

    function cap(name) { return name.charAt(0).toUpperCase() + name.slice(1); }
    function contentEl(name) { return document.getElementById('content' + cap(name)); }

    CC.activeTab = function () { return activeA; };

    CC.switchTab = function (tabName) {
        if (paneBView === tabName) return; // already visible in pane B
        VIEWS.forEach(n => {
            const tab = document.getElementById('tab' + n);
            if (tab) {
                tab.classList.remove('font-semibold', 'border-indigo-700', 'text-indigo-900', 'dark:border-indigo-400', 'dark:text-indigo-300');
                tab.classList.add('font-medium', 'border-transparent', 'text-slate-600', 'dark:text-slate-400');
            }
            const content = contentEl(n.toLowerCase());
            if (content && content.parentElement.id === 'paneA') content.classList.add('hidden');
        });
        const tab = document.getElementById('tab' + cap(tabName));
        const content = contentEl(tabName);
        if (tab && content && content.parentElement.id === 'paneA') {
            tab.classList.remove('font-medium', 'border-transparent', 'text-slate-600', 'dark:text-slate-400');
            tab.classList.add('font-semibold', 'border-indigo-700', 'text-indigo-900', 'dark:border-indigo-400', 'dark:text-indigo-300');
            content.classList.remove('hidden');
            if (tabName === 'pdf') tab.style.display = 'flex';
            activeA = tabName;
        }
        const copyBtn = document.getElementById('copyBtn');
        if (copyBtn) {
            copyBtn.style.display =
                (tabName === 'generate' && CC.getCode && CC.getCode().length > 0) ? 'flex' : 'none';
        }
        if (CC.editor && tabName === 'generate') setTimeout(() => CC.editor.refresh(), 30);
    };
    window.switchTab = CC.switchTab; // keep inline onclick handlers working

    CC.openSplit = function (viewName) {
        const paneB = document.getElementById('paneB');
        const divider = document.getElementById('paneDivider');
        const select = document.getElementById('paneBSelect');
        if (!paneB) return;
        viewName = viewName || (activeA === 'generate' ? 'results' : 'generate');
        if (viewName === activeA) viewName = activeA === 'converse' ? 'generate' : 'converse';
        CC.closeSplit();
        const content = contentEl(viewName);
        if (!content) return;
        document.getElementById('paneBContent').appendChild(content);
        content.classList.remove('hidden');
        paneB.classList.remove('hidden');
        paneB.classList.add('flex');
        divider.classList.remove('hidden');
        select.value = viewName;
        paneBView = viewName;
        const splitBtn = document.getElementById('splitBtn');
        if (splitBtn) splitBtn.setAttribute('aria-pressed', 'true');
        if (CC.editor) setTimeout(() => CC.editor.refresh(), 30);
    };

    CC.closeSplit = function () {
        const paneB = document.getElementById('paneB');
        const divider = document.getElementById('paneDivider');
        if (!paneB || paneB.classList.contains('hidden')) { paneBView = null; return; }
        if (paneBView) {
            const content = contentEl(paneBView);
            if (content) {
                document.getElementById('paneA').appendChild(content);
                if (paneBView !== activeA) content.classList.add('hidden');
            }
        }
        paneB.classList.add('hidden');
        paneB.classList.remove('flex');
        divider.classList.add('hidden');
        paneBView = null;
        const splitBtn = document.getElementById('splitBtn');
        if (splitBtn) splitBtn.setAttribute('aria-pressed', 'false');
        CC.switchTab(activeA);
    };

    function initSplitControls() {
        const splitBtn = document.getElementById('splitBtn');
        const select = document.getElementById('paneBSelect');
        const closeBtn = document.getElementById('paneBClose');
        const divider = document.getElementById('paneDivider');
        const paneA = document.getElementById('paneA');
        if (splitBtn) {
            splitBtn.addEventListener('click', () => {
                paneBView ? CC.closeSplit() : CC.openSplit();
            });
        }
        if (select) {
            select.addEventListener('change', () => {
                const v = select.value;
                if (v && v !== activeA) CC.openSplit(v);
                else select.value = paneBView || '';
            });
        }
        if (closeBtn) closeBtn.addEventListener('click', CC.closeSplit);
        if (divider && paneA) {
            let dragging = false;
            divider.addEventListener('mousedown', (e) => {
                dragging = true; divider.classList.add('dragging');
                document.body.classList.add('select-none');
                e.preventDefault();
            });
            document.addEventListener('mousemove', (e) => {
                if (!dragging) return;
                const area = document.getElementById('paneArea');
                const rect = area.getBoundingClientRect();
                let pct = ((e.clientX - rect.left) / rect.width) * 100;
                pct = Math.max(25, Math.min(75, pct));
                paneA.style.flex = `0 0 ${pct}%`;
            });
            document.addEventListener('mouseup', () => {
                dragging = false; divider.classList.remove('dragging');
                document.body.classList.remove('select-none');
            });
        }
    }

    // --- sidebar resizer ---------------------------------------------------------
    function initSidebarResizer() {
        const resizer = document.getElementById('resizer');
        const leftSidebar = document.getElementById('leftSidebar');
        if (!resizer || !leftSidebar) return;
        let isResizing = false;
        resizer.addEventListener('mousedown', (e) => {
            isResizing = true;
            document.body.style.cursor = 'col-resize';
            document.body.classList.add('select-none');
            leftSidebar.style.transition = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', (e) => {
            if (!isResizing) return;
            let w = e.clientX;
            if (w < 320) w = 320;
            if (w > window.innerWidth * 0.5) w = window.innerWidth * 0.5;
            leftSidebar.style.width = `${w}px`;
        });
        document.addEventListener('mouseup', () => {
            if (isResizing) {
                isResizing = false;
                document.body.style.cursor = '';
                document.body.classList.remove('select-none');
                leftSidebar.style.transition = '';
            }
        });
        // 5.9: keyboard-operable resizer
        resizer.addEventListener('keydown', (e) => {
            const cur = leftSidebar.getBoundingClientRect().width;
            if (e.key === 'ArrowLeft') leftSidebar.style.width = `${Math.max(320, cur - 24)}px`;
            if (e.key === 'ArrowRight') leftSidebar.style.width = `${Math.min(window.innerWidth * 0.5, cur + 24)}px`;
        });
    }

    // --- intelligent codebook (render + toggle + pop-out 6.2) ----------------------
    let codebookVisible = true;

    CC.toggleCodebook = function () {
        const list = document.getElementById('codebookList');
        const label = document.getElementById('codebookToggleLabel');
        const chevron = document.getElementById('codebookChevron');
        codebookVisible = !codebookVisible;
        list.style.display = codebookVisible ? '' : 'none';
        label.textContent = codebookVisible ? 'Hide' : 'Show';
        chevron.style.transform = codebookVisible ? 'rotate(0deg)' : 'rotate(180deg)';
    };
    window.toggleCodebook = CC.toggleCodebook;
    window.toggleTheme = CC.toggleTheme;

    CC.renderCodebookUI = function () {
        const cbContainer = document.getElementById('codebookContainer');
        const cbList = document.getElementById('codebookList');
        cbList.innerHTML = '';
        const codebook = CC.state.codebook;
        if (!codebook || Object.keys(codebook).length === 0) return;

        const orderedKeys = CC.state.headers && CC.state.headers.length > 0
            ? CC.state.headers.filter(h => Object.prototype.hasOwnProperty.call(codebook, h))
            : Object.keys(codebook);

        orderedKeys.forEach(colName => {
            const classification = codebook[colName];
            const btn = document.createElement('div');
            let colorClasses = 'bg-slate-200 text-slate-900 dark:bg-slate-700/50 dark:text-slate-200 border-slate-400 dark:border-slate-600';
            if (classification === 'Continuous') {
                colorClasses = 'bg-blue-100 text-blue-900 border-blue-300 dark:bg-blue-900/20 dark:text-blue-300 dark:border-blue-800/30';
            } else if (classification === 'Nominal') {
                colorClasses = 'bg-amber-100 text-amber-900 border-amber-300 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-800/30';
            } else if (classification === 'Ordinal') {
                colorClasses = 'bg-emerald-100 text-emerald-900 border-emerald-300 dark:bg-emerald-900/20 dark:text-emerald-300 dark:border-emerald-800/30';
            }
            const colLower = colName.toLowerCase();
            const matchedKey = Object.keys(CC.state.pdfMapping).find(k => k.toLowerCase() === colLower);
            const desc = matchedKey ? CC.state.pdfMapping[matchedKey] : null;
            const safeDesc = CC.escapeHtml(desc);
            const safeCol = CC.escapeHtml(colName);
            const safeClass = CC.escapeHtml(classification);
            const descHTML = desc
                ? `<div class="text-[11px] font-normal mt-1 text-slate-700 dark:text-slate-300 line-clamp-3 leading-snug w-full opacity-90 border-t border-slate-400/30 dark:border-slate-500/30 pt-1" title="${safeDesc}">${safeDesc}</div>`
                : '';
            btn.className = `px-3 py-2 rounded-lg border text-left transition-all hover:-translate-y-0.5 hover:shadow-md flex flex-col items-start cursor-pointer group ${colorClasses}`;
            btn.innerHTML = `<div class="flex justify-between w-full items-center gap-2"><span class="font-mono text-sm font-bold truncate max-w-[200px]" title="${safeCol}">${safeCol}</span> <span class="opacity-80 text-[10px] font-bold uppercase tracking-[0.2em] mt-0.5">${safeClass}</span></div>${descHTML}`;
            btn.onclick = () => CC.viewColumn(colName);
            cbList.appendChild(btn);
        });

        codebookVisible = true;
        cbList.style.display = '';
        const lbl = document.getElementById('codebookToggleLabel');
        const chev = document.getElementById('codebookChevron');
        if (lbl) lbl.textContent = 'Hide';
        if (chev) chev.style.transform = 'rotate(0deg)';
        cbContainer.classList.remove('hidden');
    };

    // Pop-out codebook (6.2): float the list in a draggable panel.
    CC.popOutCodebook = function () {
        if (document.getElementById('codebookFloat')) return;
        const list = document.getElementById('codebookList');
        const float = document.createElement('div');
        float.id = 'codebookFloat';
        float.className = 'glass-panel border border-slate-300 dark:border-slate-700/60';
        float.innerHTML =
            '<div id="codebookFloatHandle" class="cursor-move flex items-center justify-between px-4 py-2.5 border-b border-slate-300 dark:border-slate-700/50">' +
            '<span class="text-xs font-bold uppercase tracking-wider text-slate-800 dark:text-slate-300">Intelligent Codebook</span>' +
            '<button id="codebookDockBtn" class="text-xs font-mono font-bold text-indigo-700 dark:text-indigo-400 hover:underline" aria-label="Dock codebook back to sidebar">Dock</button>' +
            '</div><div class="float-body"></div>';
        document.body.appendChild(float);
        float.querySelector('.float-body').appendChild(list);
        list.style.display = '';

        document.getElementById('codebookDockBtn').addEventListener('click', CC.dockCodebook);

        // drag behavior
        const handle = document.getElementById('codebookFloatHandle');
        let drag = null;
        handle.addEventListener('mousedown', (e) => {
            const rect = float.getBoundingClientRect();
            drag = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
            document.body.classList.add('select-none');
            e.preventDefault();
        });
        document.addEventListener('mousemove', (e) => {
            if (!drag) return;
            float.style.left = `${Math.max(0, e.clientX - drag.dx)}px`;
            float.style.top = `${Math.max(0, e.clientY - drag.dy)}px`;
            float.style.right = 'auto';
        });
        document.addEventListener('mouseup', () => {
            drag = null;
            document.body.classList.remove('select-none');
        });
        document.getElementById('codebookContainer').classList.add('hidden');
    };

    CC.dockCodebook = function () {
        const float = document.getElementById('codebookFloat');
        if (!float) return;
        const list = float.querySelector('#codebookList');
        const container = document.getElementById('codebookContainer');
        container.appendChild(list);
        float.remove();
        if (Object.keys(CC.state.codebook).length > 0) container.classList.remove('hidden');
    };

    // --- workspace preferences (persisted) + sidebar collapse ----------------------
    CC.prefs = {
        get(key, dflt) {
            const v = localStorage.getItem('cc_pref_' + key);
            return v === null ? dflt : v === 'true';
        },
        set(key, val) { localStorage.setItem('cc_pref_' + key, val ? 'true' : 'false'); },
    };

    // Split view (6.1) can be hidden entirely as a focus preference.
    function applySplitPref() {
        const on = CC.prefs.get('split', true);
        const btn = document.getElementById('splitBtn');
        if (btn) btn.style.display = on ? '' : 'none';
        if (!on) CC.closeSplit();
    }

    // Collapse the left sidebar for a full-width workspace.
    CC.setSidebarCollapsed = function (collapsed) {
        const sidebar = document.getElementById('leftSidebar');
        const resizer = document.getElementById('resizer');
        const expandBtn = document.getElementById('expandSidebarBtn');
        if (!sidebar) return;
        sidebar.classList.toggle('hidden', collapsed);
        if (resizer) resizer.classList.toggle('hidden', collapsed);
        if (expandBtn) expandBtn.classList.toggle('hidden', !collapsed);
        localStorage.setItem('cc_sidebar_collapsed', collapsed ? 'true' : 'false');
        if (CC.editor) setTimeout(() => CC.editor.refresh(), 30);
    };
    CC.toggleSidebar = function () {
        const sidebar = document.getElementById('leftSidebar');
        CC.setSidebarCollapsed(!sidebar.classList.contains('hidden'));
    };
    window.toggleSidebar = CC.toggleSidebar;

    function initWorkspacePrefs() {
        const settingsBtn = document.getElementById('settingsBtn');
        const autosuggest = document.getElementById('prefAutosuggest');
        const split = document.getElementById('prefSplit');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', () => {
                if (autosuggest) autosuggest.checked = CC.prefs.get('autosuggest', true);
                if (split) split.checked = CC.prefs.get('split', true);
                CC.modal.open('settingsModal');
            });
        }
        if (autosuggest) autosuggest.addEventListener('change', (e) => CC.prefs.set('autosuggest', e.target.checked));
        if (split) split.addEventListener('change', (e) => { CC.prefs.set('split', e.target.checked); applySplitPref(); });
        applySplitPref();
        if (localStorage.getItem('cc_sidebar_collapsed') === 'true') CC.setSidebarCollapsed(true);
    }

    document.addEventListener('DOMContentLoaded', () => {
        initSplitControls();
        initSidebarResizer();
        initWorkspacePrefs();
        const popoutBtn = document.getElementById('codebookPopoutBtn');
        if (popoutBtn) {
            popoutBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                CC.popOutCodebook();
            });
        }
    });
}());
