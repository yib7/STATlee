/* Session workbook (SP1): the run rail in Results, read-only viewing of a
 * saved run, "Load script", star and inline rename, and the report builder's
 * run checklist.
 *
 * Every run field shown here (name, question, file name, model) is user or
 * model text, so this module builds its DOM with createElement + textContent
 * only. The one rendered field, a run's interpretation, goes through
 * CC.renderMarkdown (the sanitizer) in analyze.js. */
(function () {
    'use strict';

    const wb = {
        runs: [],        // GET /runs summaries, oldest first
        latest: null,    // the newest run id
        live: null,      // the run this page last executed or restored
        viewing: null,   // id of the saved run open in Results, if any
        viewed: null,    // that run's full record (GET /runs/<id>)
        picks: {},       // report checklist choices the user made: id -> bool
        editing: false,  // an inline rename is open: hold re-renders
    };
    let refreshSeq = 0;

    function byId(id) { return document.getElementById(id); }

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function runUrl(id) { return '/runs/' + encodeURIComponent(id); }

    /** "r0003" -> "3": run numbers come from the id, so they stay stable when
     *  an old run is evicted (ids are never reused). */
    function runNumber(id) {
        const n = parseInt(String(id || '').replace(/^r/, ''), 10);
        return isNaN(n) ? String(id || '') : String(n);
    }

    function runLabel(run) {
        const name = typeof run.name === 'string' ? run.name.trim() : '';
        const question = typeof run.prompt_excerpt === 'string' ? run.prompt_excerpt.trim() : '';
        return name || question || ('Run ' + runNumber(run.run_id));
    }

    function parseTime(iso) {
        const t = Date.parse(iso || '');
        return isNaN(t) ? null : t;
    }

    function shortTime(iso) {
        const t = parseTime(iso);
        if (t === null) return '';
        const d = new Date(t);
        const clock = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (d.toDateString() === new Date().toDateString()) return clock;
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + clock;
    }

    function fullTime(iso) {
        const t = parseTime(iso);
        return t === null ? '' : new Date(t).toLocaleString();
    }

    function failed(run) { return CC.savedRunFailed ? CC.savedRunFailed(run) : false; }

    function find(id) { return wb.runs.find((r) => r.run_id === id) || null; }

    function datasetLabel(filename) {
        if (!filename) return '';
        return filename === CC.state.filename && CC.datasetLabel ? CC.datasetLabel() : filename;
    }

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const STAR = 'M12 3.6l2.55 5.2 5.75.84-4.15 4.05.98 5.71L12 16.7l-5.13 2.7.98-5.71L3.7 9.64l5.75-.84z';
    const PENCIL = 'M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4';
    function icon(d, filled) {
        const svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '16');
        svg.setAttribute('height', '16');
        svg.setAttribute('fill', filled ? 'currentColor' : 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '1.9');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', d);
        svg.appendChild(path);
        return svg;
    }

    /** The row Results is showing: the saved run open from the rail, else the
     *  run this page executed (or restored) last. */
    function currentId() { return wb.viewing || wb.live; }

    // =====================================================================
    // Run rail (Results tab)
    // =====================================================================
    function runRow(run, isCurrent) {
        const n = runNumber(run.run_id);
        const li = el('li', 'wb-run');
        li.dataset.runId = run.run_id;

        const open = el('button', 'wb-run-open');
        open.type = 'button';
        if (isCurrent) open.setAttribute('aria-current', 'true');
        const label = runLabel(run);
        const metaParts = [];
        const time = shortTime(run.created_at);
        if (time) metaParts.push({ text: time });
        if (failed(run)) metaParts.push({ text: run.timed_out ? 'timed out' : 'failed', cls: 'wb-run-failed' });
        if (run.has_plots) metaParts.push({ text: 'plots' });
        if (run.run_id === wb.latest) metaParts.push({ text: 'latest' });
        open.setAttribute('aria-label', 'Run ' + n + ': ' + label
            + (metaParts.length ? ', ' + metaParts.map((p) => p.text).join(', ') : ''));
        const question = typeof run.prompt_excerpt === 'string' ? run.prompt_excerpt : '';
        open.title = [fullTime(run.created_at), question].filter(Boolean).join('\n');

        const num = el('span', 'wb-run-num', n);
        num.setAttribute('aria-hidden', 'true');
        const body = el('span', 'wb-run-body');
        body.setAttribute('aria-hidden', 'true');
        body.appendChild(el('span', 'wb-run-name', label));
        const meta = el('span', 'wb-run-meta');
        metaParts.forEach((part, i) => {
            if (i) meta.appendChild(document.createTextNode(' · '));
            meta.appendChild(el('span', part.cls || '', part.text));
        });
        body.appendChild(meta);
        open.append(num, body);
        open.addEventListener('click', () => view(run.run_id));

        const star = el('button', 'wb-icon-btn wb-run-star');
        star.type = 'button';
        star.setAttribute('aria-pressed', run.starred ? 'true' : 'false');
        star.setAttribute('aria-label', 'Star run ' + n);
        // 3A A3: starring picks a run for reports. It only moves the run to
        // the back of the eviction queue; a full workbook can still drop it.
        star.title = run.starred
            ? 'Starred: selected for reports by default. When the workbook is full, '
                + 'unstarred runs are removed first, but a starred run is not kept forever.'
            : 'Star this run to select it for reports';
        star.appendChild(icon(STAR, !!run.starred));
        star.addEventListener('click', () => toggleStar(run.run_id));

        const rename = el('button', 'wb-icon-btn wb-run-rename');
        rename.type = 'button';
        rename.setAttribute('aria-label', 'Rename run ' + n);
        rename.title = 'Rename';
        rename.appendChild(icon(PENCIL, false));
        rename.addEventListener('click', () => startRename(run.run_id));

        li.append(open, star, rename);
        return li;
    }

    /** Keep the current row visible inside the rail's own scroll box. */
    function revealCurrent(list) {
        const cur = list.querySelector('.wb-run-open[aria-current="true"]');
        if (!cur) return;
        const li = cur.parentElement;
        const top = li.offsetTop;
        const bottom = top + li.offsetHeight;
        if (top < list.scrollTop) list.scrollTop = top;
        else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
    }

    /** `focus` = {id, cls}: the control to give focus back to once the rows
     *  are rebuilt, so a star/rename/open by keyboard keeps its place. */
    function renderRail(focus) {
        const rail = byId('runRail');
        const list = byId('runList');
        if (!rail || !list || wb.editing) return;
        if (!wb.runs.length) {
            rail.classList.add('hidden');
            list.replaceChildren();
            return;
        }
        rail.classList.remove('hidden');
        const count = byId('runRailCount');
        if (count) count.textContent = wb.runs.length === 1 ? '1 run' : wb.runs.length + ' runs';
        const current = currentId();
        list.replaceChildren(...wb.runs.map((run) => runRow(run, run.run_id === current)));
        revealCurrent(list);
        if (focus) {
            const target = list.querySelector('li[data-run-id="' + CSS.escape(focus.id) + '"] .' + focus.cls);
            if (target) target.focus();
        }
    }

    function update(summary) {
        if (!summary || typeof summary.run_id !== 'string') return;
        const i = wb.runs.findIndex((r) => r.run_id === summary.run_id);
        if (i !== -1) wb.runs[i] = Object.assign({}, wb.runs[i], summary);
        if (wb.viewed && wb.viewed.run_id === summary.run_id) {
            wb.viewed.name = summary.name;
            wb.viewed.starred = summary.starred;
        }
    }

    async function toggleStar(id) {
        const run = find(id);
        if (!run) return;
        const next = !run.starred;
        const focus = { id, cls: 'wb-run-star' };
        run.starred = next;                       // optimistic
        renderAll(focus);
        let res;
        try { res = await CC.patch(runUrl(id), { starred: next }); } catch (e) { res = null; }
        if (!res || !res.ok) {
            const again = find(id);
            if (again) again.starred = !next;
            renderAll(focus);
            CC.toast(res ? CC.errorMessage(res.status, res.data, 'Could not update the run')
                : 'Network error: the star was not saved.', 'error');
            if (res && res.status === 404) refresh();
            return;
        }
        update(res.data);
        renderAll(focus);
        CC.announce('Run ' + runNumber(id) + (next ? ' starred.' : ' unstarred.'));
    }

    function startRename(id) {
        const list = byId('runList');
        const li = list && list.querySelector('li[data-run-id="' + CSS.escape(id) + '"]');
        const run = find(id);
        if (!li || !run || wb.editing) return;
        wb.editing = true;
        li.classList.add('editing');
        const input = el('input', 'wb-run-name-input');
        input.type = 'text';
        input.maxLength = 80;
        input.value = run.name || '';
        input.placeholder = runLabel(Object.assign({}, run, { name: null }));
        input.setAttribute('aria-label', 'Name for run ' + runNumber(id)
            + '. Enter saves, Escape cancels.');
        li.insertBefore(input, li.children[1] || null);

        let settled = false;
        const close = () => {
            wb.editing = false;
            renderAll({ id, cls: 'wb-run-rename' });
        };
        const finish = async (save) => {
            if (settled) return;
            settled = true;
            const value = input.value.trim();
            if (!save || value === (run.name || '')) { close(); return; }
            input.disabled = true;
            let res;
            try { res = await CC.patch(runUrl(id), { name: value || null }); } catch (e) { res = null; }
            if (res && res.ok) {
                update(res.data);
                CC.announce('Run ' + runNumber(id) + ' renamed.');
            } else {
                CC.toast(res ? CC.errorMessage(res.status, res.data, 'Could not rename the run')
                    : 'Network error: the name was not saved.', 'error');
            }
            close();
            if (res && res.status === 404) refresh();
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); finish(true); }
            else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
        });
        input.addEventListener('blur', () => finish(true));
        input.focus();
        input.select();
    }

    // =====================================================================
    // Viewing a saved run (read-only)
    // =====================================================================
    function renderViewBar() {
        const bar = byId('runViewBar');
        if (!bar) return;
        const run = wb.viewing ? wb.viewed : null;
        if (!run) { bar.classList.add('hidden'); return; }
        const n = runNumber(run.run_id);
        byId('runViewTitle').textContent = 'Run ' + n + (run.name ? ': ' + run.name : '');
        const meta = [];
        const time = fullTime(run.created_at);
        if (time) meta.push(time);
        if (run.filename) {
            meta.push(datasetLabel(run.filename)
                + (Number.isInteger(run.dataset_version) ? ' v' + run.dataset_version : ''));
        }
        if (run.language) meta.push(run.language);
        if (run.model) meta.push(run.model);
        byId('runViewMeta').textContent = meta.join(' · ');
        byId('runViewQuestion').textContent = run.prompt
            ? 'Question: ' + run.prompt : 'No question was recorded for this run.';
        byId('runInheritNote').classList.toggle('hidden', !run.prompt_inherited);
        const hasInterpretation = !!(run.interpretation && run.interpretation.trim());
        const hasContent = !!((run.output || '').trim() || (run.plots || []).length);
        byId('runInterpretBtn').classList.toggle('hidden', hasInterpretation || !hasContent);
        byId('runLoadScriptBtn').classList.toggle('hidden', !run.code);
        byId('runBackBtn').classList.toggle('hidden', !wb.latest || run.run_id === wb.latest);
        bar.classList.remove('hidden');
    }

    async function fetchRun(id) {
        let res;
        try { res = await CC.getJSON(runUrl(id)); } catch (e) { return { error: 'network' }; }
        if (!res.ok) return { error: res.status === 404 ? 'gone' : 'failed', res };
        return { run: res.data };
    }

    /** Open a run from the rail. An older run renders read-only and leaves
     *  the editor alone; the run this page executed last is simply shown. */
    async function view(id) {
        if (CC.resultsBusy && CC.resultsBusy()) {
            CC.toast('Wait for the current run to finish, then open another one.', 'info');
            return;
        }
        if (!wb.viewing && id === wb.live) { CC.switchTab('results'); return; }
        const got = await fetchRun(id);
        if (!got.run) {
            if (got.error === 'gone') {
                CC.toast('That run is no longer in the workbook.', 'error');
                refresh();
            } else if (got.error === 'network') {
                CC.toast('Network error: could not open that run.', 'error');
            } else {
                CC.toast(CC.errorMessage(got.res.status, got.res.data, 'Could not open the run'), 'error');
            }
            return;
        }
        if (CC.resultsBusy && CC.resultsBusy()) return;    // a run started meanwhile
        CC.renderSavedRun(got.run);
        if (id === wb.live) {
            wb.viewing = null;
            wb.viewed = null;
        } else {
            wb.viewing = id;
            wb.viewed = got.run;
        }
        renderViewBar();
        renderRail({ id, cls: 'wb-run-open' });
        CC.switchTab('results');
        CC.announce(wb.viewing ? 'Showing run ' + runNumber(id) + ', read-only.'
            : 'Showing run ' + runNumber(id) + '.');
    }

    function loadScript() {
        const run = wb.viewed;
        if (!run) return;
        if (!run.code) { CC.toast('This run has no saved script.', 'error'); return; }
        if (CC.isGenerating && CC.isGenerating()) {
            CC.toast('Wait for the script that is being generated, then load this one.', 'info');
            return;
        }
        const n = runNumber(run.run_id);
        const current = CC.getCode();
        if (current.trim() && current !== run.code
            && !window.confirm('Replace the script in the editor with the script from run ' + n + '?')) return;
        CC.loadRunScript(run);
        CC.toast('The script from run ' + n + ' is in the editor. Run it to add a new run.', 'success');
    }

    async function interpretViewed() {
        const run = wb.viewing ? wb.viewed : null;
        if (!run || (CC.resultsBusy && CC.resultsBusy())) return;
        const btn = byId('runInterpretBtn');
        if (btn) btn.disabled = true;
        try {
            const text = await CC.interpretSavedRun(run);
            if (text && wb.viewed && wb.viewed.run_id === run.run_id) wb.viewed.interpretation = text;
        } finally {
            if (btn) btn.disabled = false;
            renderViewBar();
        }
    }

    // =====================================================================
    // Report builder run checklist
    // =====================================================================
    /** Default: the latest run plus every starred run. A box the user ticked
     *  or cleared keeps that choice across refreshes. */
    function isPicked(run) {
        if (Object.prototype.hasOwnProperty.call(wb.picks, run.run_id)) return wb.picks[run.run_id];
        return run.run_id === wb.latest || !!run.starred;
    }

    function renderChecklist() {
        const box = byId('reportRuns');
        const list = byId('reportRunList');
        if (!box || !list) return;
        if (!wb.runs.length) {
            box.classList.add('hidden');
            list.replaceChildren();
            return;
        }
        box.classList.remove('hidden');
        const focused = list.contains(document.activeElement) ? document.activeElement.value : null;
        list.replaceChildren(...wb.runs.map((run) => {
            const label = el('label', 'wb-check');
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.value = run.run_id;
            input.checked = isPicked(run);
            input.addEventListener('change', () => { wb.picks[run.run_id] = input.checked; });
            const meta = [];
            if (run.starred) meta.push('starred');
            if (run.run_id === wb.latest) meta.push('latest');
            if (failed(run)) meta.push(run.timed_out ? 'timed out' : 'failed');
            const time = shortTime(run.created_at);
            if (time) meta.push(time);
            if (run.filename) meta.push(datasetLabel(run.filename));
            label.append(input,
                el('span', 'wb-check-num', runNumber(run.run_id)),
                el('span', 'wb-check-name', runLabel(run)),
                el('span', 'wb-check-meta', meta.join(' · ')));
            return label;
        }));
        if (focused) {
            const again = list.querySelector('input[value="' + CSS.escape(focused) + '"]');
            if (again) again.focus();
        }
    }

    function renderAll(focus) {
        renderRail(focus);
        renderChecklist();
        renderViewBar();
    }

    // =====================================================================
    // Sync with the server
    // =====================================================================
    async function refresh() {
        const seq = ++refreshSeq;
        let res;
        try { res = await CC.getJSON('/runs'); } catch (e) { return; }
        // Anonymous visitors behind REQUIRE_LOGIN get a 401: no workbook yet.
        if (seq !== refreshSeq || !res.ok) return;
        const data = res.data || {};
        wb.runs = Array.isArray(data.runs)
            ? data.runs.filter((r) => r && typeof r.run_id === 'string') : [];
        wb.latest = typeof data.latest === 'string' ? data.latest : null;
        const ids = new Set(wb.runs.map((r) => r.run_id));
        Object.keys(wb.picks).forEach((id) => { if (!ids.has(id)) delete wb.picks[id]; });
        if (wb.live && !ids.has(wb.live)) wb.live = null;
        if (wb.viewing && !ids.has(wb.viewing)) {
            // The run on view left the workbook (evicted, or a sign-in
            // swapped the workbook): end the read-only view.
            endView();
            CC.announce('That run is no longer in the workbook.');
        }
        renderAll();
    }

    function endView() {
        wb.viewing = null;
        wb.viewed = null;
    }

    /** Tell the user when a new run pushed older ones out of the workbook,
     *  naming any starred one (3A A3). Labels are read before the refresh
     *  drops the evicted rows. */
    function announceEvictions(evicted) {
        const gone = Array.isArray(evicted) ? evicted.filter((r) => r && r.run_id) : [];
        if (!gone.length) return;
        const label = (r) => {
            const name = typeof r.name === 'string' ? r.name.trim() : '';
            const known = find(r.run_id);
            return name || (known ? runLabel(known) : 'Run ' + runNumber(r.run_id));
        };
        let msg = gone.length === 1
            ? 'An older run was removed to make room: the workbook is full.'
            : gone.length + ' older runs were removed to make room: the workbook is full.';
        const starred = gone.filter((r) => r.starred).map((r) => '"' + label(r) + '"');
        if (starred.length) {
            msg += ' This included starred run' + (starred.length > 1 ? 's ' : ' ')
                + starred.join(', ') + '. Starring picks runs for reports; it does not keep them.';
        }
        CC.toast(msg, 'info');
    }

    CC.workbook = {
        refresh,
        view,
        fetchRun,
        /** A run this page just executed or restored: it becomes the current
         *  row, and any saved-run view ends. */
        /** `evicted` is /run's list of runs this save pushed out. */
        noteLiveRun(id, evicted) {
            announceEvictions(evicted);
            wb.live = id || null;
            wb.viewing = null;
            wb.viewed = null;
            renderViewBar();
            return refresh();
        },
        /** A new run is taking over Results. */
        leaveView() {
            endView();
            renderViewBar();
            renderRail();
        },
        /** A new dataset: nothing stays open, the runs stay listed. */
        reset() {
            wb.live = null;
            wb.viewing = null;
            wb.viewed = null;
            renderViewBar();
            renderRail();
        },
        /** The run the user is looking at: a saved one, else the live one. */
        activeRunId() { return wb.viewing || wb.live || null; },
        viewedRun() { return wb.viewing ? wb.viewed : null; },
        selectedRunIds() { return wb.runs.filter(isPicked).map((r) => r.run_id); },
        count() { return wb.runs.length; },
    };

    document.addEventListener('DOMContentLoaded', () => {
        const load = byId('runLoadScriptBtn');
        if (load) load.addEventListener('click', loadScript);
        const back = byId('runBackBtn');
        if (back) back.addEventListener('click', () => { if (wb.latest) view(wb.latest); });
        const interp = byId('runInterpretBtn');
        if (interp) interp.addEventListener('click', interpretViewed);
        refresh();
    });
}());
