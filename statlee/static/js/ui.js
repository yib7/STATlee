/* UI chrome: theme, modals, pipeline checklist, tab + split-pane
 * workspace, sidebar resizer, codebook rendering + pop-out. */
(function () {
    'use strict';

    // --- guarded storage -----------------------------------------------
    // Blocked storage (privacy modes, enterprise policy) throws on access. It
    // must degrade to "no persistence", never abort start-up or the upload flow.
    CC.store = {
        get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
        set(key, val) { try { window.localStorage.setItem(key, val); return true; } catch (e) { return false; } },
    };

    // --- theme -------------------------------------------------------------
    CC.toggleTheme = function () {
        document.documentElement.classList.toggle('dark');
        CC.store.set('theme',
            document.documentElement.classList.contains('dark') ? 'dark' : 'light');
    };

    // --- modal helpers ---------------------------------------------------
    // open() remembers the opener, moves focus into the dialog and traps Tab
    // inside it; close() restores focus. dismiss() is the USER closing it
    // (Escape, the X): it also runs the dialog's onDismiss hook, so a dialog
    // whose close has a meaning (the codebook prompt: "no thanks" = skip) can
    // never be closed into a stuck state. Forced dialogs refuse it.
    const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]),' +
        ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const openers = {};
    const dismissHooks = {};

    function visibleFocusables(root) {
        return Array.prototype.filter.call(root.querySelectorAll(FOCUSABLE),
            el => el.offsetParent !== null || el === document.activeElement);
    }
    function topModal() {
        const open = document.querySelectorAll('.cc-modal:not(.hidden)');
        return open.length ? open[open.length - 1] : null;
    }

    CC.modal = {
        open(id) {
            const el = document.getElementById(id);
            if (!el) return;
            if (el.classList.contains('hidden')) {
                const active = document.activeElement;
                openers[id] = (active && active !== document.body) ? active : null;
            }
            el.classList.remove('hidden');
            setTimeout(() => {
                if (el.classList.contains('hidden') || el.contains(document.activeElement)) return;
                const items = visibleFocusables(el);
                const first = items.find(n => !n.hasAttribute('data-close-modal')) || items[0];
                if (first) first.focus();
            }, 30);
        },
        close(id) {
            const el = document.getElementById(id);
            if (!el || el.classList.contains('hidden')) { if (el) el.classList.add('hidden'); return; }
            el.classList.add('hidden');
            const opener = openers[id];
            delete openers[id];
            if (opener && document.contains(opener) && !topModal()) {
                try { opener.focus(); } catch (e) { /* detached */ }
            }
        },
        dismiss(id) {
            const el = document.getElementById(id);
            if (!el || el.classList.contains('hidden') || el.dataset.forced) return;
            CC.modal.close(id);
            if (dismissHooks[id]) dismissHooks[id]();
        },
        onDismiss(id, fn) { dismissHooks[id] = fn; },
    };
    document.addEventListener('click', (e) => {
        const closer = e.target.closest('[data-close-modal]');
        if (closer) CC.modal.dismiss(closer.getAttribute('data-close-modal'));
    });
    document.addEventListener('keydown', (e) => {
        const modal = topModal();
        if (!modal) return;
        if (e.key === 'Escape') {
            CC.modal.dismiss(modal.id);
            return;
        }
        if (e.key !== 'Tab') return;
        const items = visibleFocusables(modal);
        if (!items.length) { e.preventDefault(); return; }
        const first = items[0];
        const last = items[items.length - 1];
        const active = document.activeElement;
        if (!modal.contains(active)) {
            e.preventDefault();
            (e.shiftKey ? last : first).focus();
        } else if (e.shiftKey && active === first) {
            e.preventDefault(); last.focus();
        } else if (!e.shiftKey && active === last) {
            e.preventDefault(); first.focus();
        }
    });

    // --- pipeline status checklist ---------------------------------------
    // The full run spine, not just the upload half: the last two steps are the
    // ones a run can fail on, so `execute` carries recovery actions.
    const PIPELINE_STEPS = [
        { id: 'load', label: 'Data loaded' },
        { id: 'codebook_link', label: 'Codebook linked' },
        { id: 'classify', label: 'Variables classified' },
        { id: 'suggest', label: 'Suggestions ready' },
        { id: 'generate', label: 'Generate script' },
        { id: 'execute', label: 'Execute in sandbox', retry: true },
    ];

    // Recovery buttons rendered on a failed retryable step. They reach their
    // handlers through boot.js's delegated data-action listener (CSP forbids
    // inline onclick), which resolves window.retryRun / window.fixWithAI at
    // click time; analyze.js defines both.
    const RETRY_BTN =
        '<button type="button" class="step-retry" data-action="retryRun" ' +
        'title="Run the same script again">Retry</button>';
    const FIX_BTN =
        '<button type="button" class="step-fix" data-action="fixWithAI" ' +
        'title="Send the error back to STATlee and regenerate">Fix with AI</button>';

    CC.pipeline = {
        /** `status` is the note beside the PIPELINE eyebrow. It reads
         *  "awaiting data" on the empty workspace (see bootPipeline) and
         *  "in progress" once an upload actually starts the run. */
        begin(status) {
            const box = document.getElementById('pipelineChecklist');
            if (!box) return;
            const statusEl = document.getElementById('pipelineStatus');
            if (statusEl) statusEl.textContent = status || 'in progress';
            box.classList.remove('hidden');
            box.innerHTML = PIPELINE_STEPS.map(s =>
                `<div class="pipeline-step" data-step="${s.id}" data-state="pending">` +
                `<span class="step-dot" aria-hidden="true"></span>` +
                `<span>${s.label}</span><span class="step-note font-normal normal-case tracking-normal opacity-80"></span>` +
                `<span class="step-actions"></span></div>`
            ).join('');
        },
        /** `opts.noFix`: the failure never reached the sandbox (run-guard
         *  refusal, timeout, 5xx), so there is no traceback for "Fix with AI"
         *  to send; offer Retry only. */
        set(stepId, state, note, opts) {
            const el = document.querySelector(`#pipelineChecklist [data-step="${stepId}"]`);
            if (!el) return;
            el.dataset.state = state;
            const dot = el.querySelector('.step-dot');
            const CHECK = '<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 13l4 4L19 7"/></svg>';
            const CROSS = '<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
            if (state === 'done') dot.innerHTML = CHECK;
            else if (state === 'failed') dot.innerHTML = CROSS;
            else dot.innerHTML = '';
            el.querySelector('.step-note').textContent = note ? ` · ${note}` : '';
            const actions = el.querySelector('.step-actions');
            if (actions) {
                const step = PIPELINE_STEPS.find(s => s.id === stepId);
                actions.innerHTML = (state === 'failed' && step && step.retry)
                    ? RETRY_BTN + ((opts && opts.noFix) ? '' : FIX_BTN) : '';
            }
        },
    };

    // --- tabs + split panes ------------------------------------------------
    const VIEWS = ['Generate', 'Data', 'Results', 'Converse', 'Report', 'Pdf'];
    let activeA = 'generate';
    let paneBView = null;

    function cap(name) { return name.charAt(0).toUpperCase() + name.slice(1); }
    function contentEl(name) { return document.getElementById('content' + cap(name)); }

    CC.switchTab = function (tabName) {
        if (paneBView === tabName) return; // already visible in pane B
        VIEWS.forEach(n => {
            const tab = document.getElementById('tab' + n);
            if (tab) {
                tab.classList.remove('font-semibold', 'border-indigo-700', 'text-indigo-900', 'dark:border-indigo-400', 'dark:text-indigo-300');
                tab.classList.add('font-medium', 'border-transparent', 'text-slate-600', 'dark:text-slate-400');
                tab.setAttribute('aria-selected', 'false');
                tab.setAttribute('tabindex', '-1');
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
        // Roving tabindex: exactly one tab is in the Tab order, the
        // selected one; arrow keys move between the rest (initTabKeys).
        const selected = document.getElementById('tab' + cap(activeA));
        if (selected) {
            selected.setAttribute('aria-selected', 'true');
            selected.setAttribute('tabindex', '0');
        }
        const copyBtn = document.getElementById('copyBtn');
        if (copyBtn) {
            copyBtn.style.display =
                (tabName === 'generate' && CC.getCode && CC.getCode().length > 0) ? 'flex' : 'none';
        }
        if (CC.editor && tabName === 'generate') setTimeout(() => CC.editor.refresh(), 30);
    };
    window.switchTab = CC.switchTab; // boot.js's data-action dispatch resolves it on window

    /** WAI-ARIA tabs keyboard model: Left/Right (and Home/End) move focus and
     *  select; a tab whose view sits in the split pane is skipped. */
    function initTabKeys() {
        const list = document.querySelector('[role="tablist"]');
        if (!list) return;
        list.addEventListener('keydown', (e) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
            const tabs = Array.from(list.querySelectorAll('[role="tab"]'))
                .filter(t => t.getAttribute('aria-disabled') !== 'true' && t.offsetParent !== null);
            if (!tabs.length) return;
            const i = Math.max(0, tabs.indexOf(document.activeElement));
            let next = i;
            if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
            if (e.key === 'ArrowRight') next = (i + 1) % tabs.length;
            if (e.key === 'Home') next = 0;
            if (e.key === 'End') next = tabs.length - 1;
            e.preventDefault();
            const target = tabs[next];
            CC.switchTab(target.getAttribute('data-arg'));
            target.focus();
        });
    }

    // Grey out the header tab whose view currently lives in the split pane, so
    // it's obvious it's already shown elsewhere (reduces friction).
    function updateSplitTabState() {
        VIEWS.forEach(n => {
            const tab = document.getElementById('tab' + n);
            if (!tab) return;
            const inSplit = paneBView === n.toLowerCase();
            tab.classList.toggle('opacity-40', inSplit);
            tab.classList.toggle('cursor-not-allowed', inSplit);
            if (inSplit) tab.setAttribute('aria-disabled', 'true');
            else tab.removeAttribute('aria-disabled');
        });
    }

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
        updateSplitTabState();
        const splitBtn = document.getElementById('splitBtn');
        if (splitBtn) splitBtn.setAttribute('aria-pressed', 'true');
        if (CC.editor) setTimeout(() => CC.editor.refresh(), 30);
    };

    CC.closeSplit = function () {
        const paneB = document.getElementById('paneB');
        const divider = document.getElementById('paneDivider');
        if (!paneB || paneB.classList.contains('hidden')) { paneBView = null; updateSplitTabState(); return; }
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
        updateSplitTabState();
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
        // role=separator carries its position: the sidebar's share of the
        // window, in percent, kept current as it is dragged or arrowed.
        const syncValue = () => {
            const pct = leftSidebar.getBoundingClientRect().width / window.innerWidth * 100;
            resizer.setAttribute('aria-valuenow', String(Math.round(pct)));
        };
        syncValue();
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
            syncValue();
        });
        document.addEventListener('mouseup', () => {
            if (isResizing) {
                isResizing = false;
                document.body.style.cursor = '';
                document.body.classList.remove('select-none');
                leftSidebar.style.transition = '';
            }
        });
        // Keyboard-operable resizer
        resizer.addEventListener('keydown', (e) => {
            const cur = leftSidebar.getBoundingClientRect().width;
            if (e.key === 'ArrowLeft') leftSidebar.style.width = `${Math.max(320, cur - 24)}px`;
            if (e.key === 'ArrowRight') leftSidebar.style.width = `${Math.min(window.innerWidth * 0.5, cur + 24)}px`;
            syncValue();
        });
    }

    // --- codebook (render + toggle + pop-out) --------------------------
    let codebookVisible = true;
    let codebookExpanded = false;  // expanded view shows each variable's description inline

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

    // Density toggle: compact chips (hover for description) vs. expanded rows that
    // show every variable's description inline, no hovering required.
    CC.toggleCodebookDensity = function () {
        const list = document.getElementById('codebookList');
        const label = document.getElementById('codebookExpandLabel');
        codebookExpanded = !codebookExpanded;
        if (list) list.classList.toggle('expanded', codebookExpanded);
        if (label) label.textContent = codebookExpanded ? 'Compact' : 'Expand';
    };

    /** `opts.keepState`: a codebook edit or refresh (codebook.js) re-renders
     *  the chips without re-opening a list the user hid or popped out. */
    CC.renderCodebookUI = function (opts) {
        const keepState = !!(opts && opts.keepState);
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
            const safeCol = CC.escapeHtml(colName);
            // Abbreviate the classification so two chips fit per row.
            const ABBR = { Continuous: 'Cont', Nominal: 'Nom', Ordinal: 'Ord' };
            const safeClass = CC.escapeHtml(ABBR[classification] || classification);
            // Compact view hides the description behind a hover tooltip; the
            // expanded view (CSS .expanded) reveals the .codebook-desc line below.
            btn.title = desc ? `${colName}: ${desc}` : colName;
            btn.className = `codebook-chip px-2.5 py-1.5 rounded-lg border text-left transition-all hover:shadow-md flex flex-col gap-1 cursor-pointer group ${colorClasses}`;
            const descHtml = desc
                ? `<p class="codebook-desc text-[11px] leading-snug opacity-80">${CC.escapeHtml(desc)}</p>`
                : `<p class="codebook-desc text-[11px] leading-snug italic opacity-50">No description yet. Add a codebook to fill this in.</p>`;
            btn.innerHTML =
                `<div class="flex items-center justify-between gap-1.5 w-full">` +
                    `<span class="font-mono text-xs font-bold truncate">${safeCol}</span>` +
                    `<span class="opacity-80 text-[9px] font-bold uppercase tracking-[0.12em] shrink-0">${safeClass}</span>` +
                `</div>` +
                descHtml;
            btn.onclick = () => CC.viewColumn(colName);
            cbList.appendChild(btn);
        });
        cbList.classList.toggle('expanded', codebookExpanded);  // preserve density across re-renders

        if (keepState) {
            if (!document.getElementById('codebookFloat')) cbContainer.classList.remove('hidden');
            return;
        }
        codebookVisible = true;
        cbList.style.display = '';
        const lbl = document.getElementById('codebookToggleLabel');
        const chev = document.getElementById('codebookChevron');
        if (lbl) lbl.textContent = 'Hide';
        if (chev) chev.style.transform = 'rotate(0deg)';
        cbContainer.classList.remove('hidden');
    };

    // Pop-out codebook: float the list in a draggable panel.
    CC.popOutCodebook = function () {
        if (document.getElementById('codebookFloat')) return;
        const list = document.getElementById('codebookList');
        const float = document.createElement('div');
        float.id = 'codebookFloat';
        float.setAttribute('role', 'region');
        float.setAttribute('aria-label', 'Codebook');
        float.className = 'glass-panel border border-slate-300 dark:border-slate-700/60';
        float.innerHTML =
            '<div id="codebookFloatHandle" class="cursor-move flex items-center justify-between px-4 py-2.5 border-b border-slate-300 dark:border-slate-700/50">' +
            '<span class="text-xs font-bold uppercase tracking-wider text-slate-800 dark:text-slate-300">Codebook</span>' +
            '<button id="codebookDockBtn" class="text-xs font-mono font-bold text-indigo-700 dark:text-indigo-400 hover:underline" aria-label="Dock codebook back to sidebar">Dock</button>' +
            '</div><div class="float-body"></div>';
        document.body.appendChild(float);
        float.querySelector('.float-body').appendChild(list);
        list.style.display = '';

        document.getElementById('codebookDockBtn').addEventListener('click', CC.dockCodebook);
        makeDraggable(float, document.getElementById('codebookFloatHandle'));
        document.getElementById('codebookContainer').classList.add('hidden');
    };

    /** Drag a floating panel by its handle. The two document listeners are
     *  removed when the panel docks, so a pop-out/dock cycle does not
     *  leave another pair behind. */
    function makeDraggable(float, handle) {
        let drag = null;
        const onMove = (e) => {
            if (!drag) return;
            float.style.left = `${Math.max(0, e.clientX - drag.dx)}px`;
            float.style.top = `${Math.max(0, e.clientY - drag.dy)}px`;
            float.style.right = 'auto';
        };
        const onUp = () => {
            if (!drag) return;
            drag = null;
            document.body.classList.remove('select-none');
        };
        handle.addEventListener('mousedown', (e) => {
            if (e.target.closest('button')) return;      // the Dock button is not a drag
            const rect = float.getBoundingClientRect();
            drag = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
            document.body.classList.add('select-none');
            e.preventDefault();
        });
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
        float._undrag = () => {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };
    }
    function removeFloat(float) {
        if (float._undrag) float._undrag();
        float.remove();
    }

    CC.dockCodebook = function () {
        const float = document.getElementById('codebookFloat');
        if (!float) return;
        const list = float.querySelector('#codebookList');
        const container = document.getElementById('codebookContainer');
        container.appendChild(list);
        removeFloat(float);
        if (Object.keys(CC.state.codebook).length > 0) container.classList.remove('hidden');
    };

    // --- data cleaning: hide/show + pop-out (mirrors the codebook controls) ----------
    let wrangleVisible = true;

    CC.toggleWrangle = function () {
        const body = document.getElementById('wrangleBody');
        const label = document.getElementById('wrangleToggleLabel');
        const chevron = document.getElementById('wrangleChevron');
        if (!body) return;
        wrangleVisible = !wrangleVisible;
        body.style.display = wrangleVisible ? '' : 'none';
        if (label) label.textContent = wrangleVisible ? 'Hide' : 'Show';
        if (chevron) chevron.style.transform = wrangleVisible ? 'rotate(0deg)' : 'rotate(180deg)';
    };

    // Undo / Redo / Original live in the sidebar panel's header, which is
    // hidden while the cleaning tool floats: they move into the float's
    // header with it (same elements, so their handlers and state come along)
    // and back on dock.
    const VERSION_BUTTONS = ['undoBtn', 'redoBtn', 'revertBtn'];
    function moveVersionButtons(target, before) {
        if (!target) return;
        VERSION_BUTTONS.forEach((id) => {
            const b = document.getElementById(id);
            if (b) target.insertBefore(b, before || null);
        });
    }

    CC.popOutWrangle = function () {
        if (document.getElementById('wrangleFloat')) return;
        const body = document.getElementById('wrangleBody');
        if (!body) return;
        const float = document.createElement('div');
        float.id = 'wrangleFloat';
        // A floating panel outside the sidebar: a named region keeps it a landmark.
        float.setAttribute('role', 'region');
        float.setAttribute('aria-label', 'Data cleaning');
        float.className = 'glass-panel border border-slate-300 dark:border-slate-700/60';
        float.innerHTML =
            '<div id="wrangleFloatHandle" class="cursor-move flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 border-b border-slate-300 dark:border-slate-700/50">' +
            '<span class="text-xs font-bold uppercase tracking-wider whitespace-nowrap text-slate-800 dark:text-slate-300">Data Cleaning</span>' +
            '<div class="flex items-center gap-1.5 shrink-0">' +
            '<span id="wrangleFloatVersions" class="flex items-center gap-1.5"></span>' +
            '<button id="wrangleDockBtn" class="text-xs font-mono font-bold text-indigo-700 dark:text-indigo-400 hover:underline" aria-label="Dock data cleaning back to sidebar">Dock</button>' +
            '</div></div><div class="float-body"></div>';
        document.body.appendChild(float);
        float.querySelector('.float-body').appendChild(body);
        moveVersionButtons(document.getElementById('wrangleFloatVersions'));
        body.style.display = '';
        wrangleVisible = true;
        const lbl = document.getElementById('wrangleToggleLabel');
        const chev = document.getElementById('wrangleChevron');
        if (lbl) lbl.textContent = 'Hide';
        if (chev) chev.style.transform = 'rotate(0deg)';

        document.getElementById('wrangleDockBtn').addEventListener('click', CC.dockWrangle);
        makeDraggable(float, document.getElementById('wrangleFloatHandle'));
        document.getElementById('changelogPanel').classList.add('hidden');
    };

    CC.dockWrangle = function () {
        const float = document.getElementById('wrangleFloat');
        if (!float) return;
        const body = float.querySelector('#wrangleBody');
        const panel = document.getElementById('changelogPanel');
        const popout = document.getElementById('wranglePopoutBtn');
        if (popout) moveVersionButtons(popout.parentElement, popout);
        panel.appendChild(body);
        removeFloat(float);
        panel.classList.remove('hidden');
    };

    // --- workspace preferences (persisted) + sidebar collapse ----------------------
    CC.prefs = {
        get(key, dflt) {
            const v = CC.store.get('cc_pref_' + key);
            return v === null ? dflt : v === 'true';
        },
        set(key, val) { CC.store.set('cc_pref_' + key, val ? 'true' : 'false'); },
    };

    // Split view can be hidden entirely as a focus preference.
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
        CC.store.set('cc_sidebar_collapsed', collapsed ? 'true' : 'false');
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
        if (CC.store.get('cc_sidebar_collapsed') === 'true') CC.setSidebarCollapsed(true);
    }

    // The empty workspace shows the spine up front rather than materialising it
    // on upload: the run is legible before you commit a file. Step 1 carries the
    // active pulse because it is what the app is waiting on.
    function bootPipeline() {
        CC.pipeline.begin('awaiting data');
        CC.pipeline.set('load', 'active', 'waiting for a dataset');
    }

    document.addEventListener('DOMContentLoaded', () => {
        bootPipeline();
        CC.switchTab('generate');   // seeds aria-selected + the roving tabindex
        initTabKeys();
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
        const expandBtn = document.getElementById('codebookExpandBtn');
        if (expandBtn) {
            expandBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                CC.toggleCodebookDensity();
            });
        }
        const wrangleToggleBtn = document.getElementById('wrangleToggleBtn');
        if (wrangleToggleBtn) wrangleToggleBtn.addEventListener('click', CC.toggleWrangle);
        const wranglePopoutBtn = document.getElementById('wranglePopoutBtn');
        if (wranglePopoutBtn) {
            wranglePopoutBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                CC.popOutWrangle();
            });
        }
    });
}());
