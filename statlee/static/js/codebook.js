/* Survey-native codebook (SP2): the Codebook tab's variable editor.
 *
 * The server codebook is the authority (GET /codebook): one editable row per
 * column of the active dataset version, in dataset order (the response's
 * `columns` list; JSON object keys come back sorted). An edit is queued per
 * column and field and sent, debounced, as a PUT carrying ONLY the changed
 * fields, because every field a PUT names becomes a user field that later
 * seeds never overwrite. A failed save puts the field back and says why.
 *
 * Labels, value labels and column names are user, data-file or model text,
 * so this module builds its DOM with createElement + textContent only.
 *
 * The tab's other view, the attached PDF/TXT document, stays in data.js;
 * this module only switches between the two. */
(function () {
    'use strict';

    const SAVE_DELAY = 500;          // ms after the last keystroke
    const FILTER_DELAY = 120;
    const IMPORT_MAX_BYTES = 5 * 1024 * 1024;
    const TYPE_LABELS = {
        continuous: 'Continuous', ordinal: 'Ordinal', nominal: 'Nominal', binary: 'Binary',
        text: 'Text', id: 'ID', date: 'Date',
    };
    const ROLE_LABELS = {
        analyze: 'Analyze', id: 'ID (not modelled)', weight: 'Survey weight', ignore: 'Ignore',
    };
    const SOURCE_BADGES = {
        user: 'edited', native: 'data file', pdf: 'document', llm: 'model', default: 'not set',
    };
    const FIELD_NAMES = {
        type: 'type', label: 'label', value_labels: 'value labels',
        missing_codes: 'missing codes', role: 'role', scale_group: 'scale group',
    };

    const cb = {
        filename: '',            // the dataset the rows belong to
        columns: [],             // dataset order
        entries: {},             // column -> merged entry, as last read from the server
        types: Object.keys(TYPE_LABELS),
        roles: Object.keys(ROLE_LABELS),
        rows: new Map(),         // column -> row refs
        pending: {},             // column -> {field: value}, not sent yet
        pendingFile: '',         // the dataset those edits belong to
        inflight: null,          // the batch being saved
        inflightPromise: null,
        again: false,            // a flush was asked for while one was in flight
        timer: null,
        seq: 0,
        loadError: '',
        filterTimer: null,
    };

    function byId(id) { return document.getElementById(id); }

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

    // =====================================================================
    // Entry shape
    // =====================================================================
    function normalize(raw) {
        const e = raw && typeof raw === 'object' ? raw : {};
        const vl = e.value_labels && typeof e.value_labels === 'object' && !Array.isArray(e.value_labels)
            ? e.value_labels : {};
        const labels = {};
        Object.keys(vl).forEach((k) => { labels[String(k)] = vl[k] === null ? '' : String(vl[k]); });
        return {
            type: typeof e.type === 'string' && e.type ? e.type : null,
            label: typeof e.label === 'string' ? e.label : '',
            value_labels: labels,
            missing_codes: Array.isArray(e.missing_codes) ? e.missing_codes.map(String) : [],
            role: typeof e.role === 'string' && e.role ? e.role : 'analyze',
            scale_group: typeof e.scale_group === 'string' && e.scale_group ? e.scale_group : null,
            source: typeof e.source === 'string' ? e.source : 'default',
            user_fields: Array.isArray(e.user_fields) ? e.user_fields.map(String) : [],
            field_sources: e.field_sources && typeof e.field_sources === 'object' ? e.field_sources : {},
        };
    }

    /** Numeric codes in numeric order, then the rest as text. */
    function sortedCodes(labels) {
        return Object.keys(labels).sort((a, b) => {
            const x = Number(a);
            const y = Number(b);
            const nx = a.trim() !== '' && isFinite(x);
            const ny = b.trim() !== '' && isFinite(y);
            if (nx && ny) return x - y;
            if (nx !== ny) return nx ? -1 : 1;
            return a < b ? -1 : a > b ? 1 : 0;
        });
    }

    function parseCodes(text) {
        const out = [];
        String(text || '').split(',').forEach((part) => {
            const code = part.trim();
            if (code && out.indexOf(code) === -1) out.push(code);
        });
        return out;
    }

    function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

    function sameLabels(a, b) {
        const ka = Object.keys(a || {}).sort();
        const kb = Object.keys(b || {}).sort();
        return same(ka, kb) && ka.every((k) => a[k] === b[k]);
    }

    function equalField(field, a, b) {
        return field === 'value_labels' ? sameLabels(a, b) : same(a, b);
    }

    // =====================================================================
    // Rows
    // =====================================================================
    function select(cls, options, col, name) {
        const node = el('select', 'cb-input ' + cls);
        options.forEach(([value, text, disabled]) => {
            const opt = el('option', '', text);
            opt.value = value;
            if (disabled) opt.disabled = true;
            node.appendChild(opt);
        });
        node.setAttribute('aria-label', name + col);
        return node;
    }

    function input(cls, col, name, max, placeholder) {
        const node = el('input', 'cb-input ' + cls);
        node.type = 'text';
        node.maxLength = max;
        node.autocomplete = 'off';
        node.spellcheck = false;
        if (placeholder) node.placeholder = placeholder;
        node.setAttribute('aria-label', name + col);
        return node;
    }

    /** A visible field name for the stacked (narrow) layout; the control's
     *  own aria-label names its column for screen readers. */
    function field(cls, name, control) {
        const wrap = el('label', 'cb-field ' + cls);
        const caption = el('span', 'cb-field-name', name);
        caption.setAttribute('aria-hidden', 'true');
        wrap.append(caption, control);
        return wrap;
    }

    function buildRow(col, index) {
        const li = el('li', 'cb-row');
        li.dataset.col = col;

        const nameCell = el('div', 'cb-cell cb-name-cell');
        const name = el('span', 'cb-name', col);
        name.title = col;
        const badge = el('span', 'cb-badge');
        nameCell.append(name, badge);

        const type = select('cb-type',
            [['', 'Not set', true]].concat(cb.types.map((t) => [t, TYPE_LABELS[t] || t])),
            col, 'Type for ');
        const label = input('cb-label', col, 'Label for ', 2000, 'Add a label');
        const role = select('cb-role', cb.roles.map((r) => [r, ROLE_LABELS[r] || r]), col, 'Role for ');
        const scale = input('cb-scale', col, 'Scale group for ', 80, 'None');
        const missing = input('cb-missing', col, 'Missing codes for ', 400, 'e.g. -9, 99');

        const toggle = el('button', 'cb-values-toggle');
        toggle.type = 'button';
        const panelId = 'cbValues' + index;
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-controls', panelId);
        toggle.setAttribute('aria-label', 'Value labels for ' + col);
        const toggleText = el('span', 'cb-values-count');
        const chevron = el('span', 'cb-chevron', '▾');
        chevron.setAttribute('aria-hidden', 'true');
        toggle.append(toggleText, chevron);
        const valuesCell = el('div', 'cb-cell cb-values-cell');
        valuesCell.appendChild(toggle);

        const panel = el('div', 'cb-values hidden');
        panel.id = panelId;
        panel.setAttribute('role', 'group');
        panel.setAttribute('aria-label', 'Value labels for ' + col);

        const refs = {
            col, li, name, badge, type, label, role, scale, missing, toggle, toggleText, panel,
            fields: {
                type: field('cb-f-type', 'Type', type),
                label: field('cb-f-label', 'Label', label),
                role: field('cb-f-role', 'Role', role),
                scale_group: field('cb-f-scale', 'Scale group', scale),
                missing_codes: field('cb-f-missing', 'Missing codes', missing),
            },
        };
        li.append(nameCell, refs.fields.type, refs.fields.label, refs.fields.role,
            refs.fields.scale_group, refs.fields.missing_codes, valuesCell, panel);

        type.addEventListener('change', () => { if (type.value) edit(col, 'type', type.value, true); });
        role.addEventListener('change', () => edit(col, 'role', role.value, true));
        label.addEventListener('input', () => edit(col, 'label', label.value.trim()));
        scale.addEventListener('input', () => edit(col, 'scale_group', scale.value.trim() || null));
        missing.addEventListener('input', () => edit(col, 'missing_codes', parseCodes(missing.value)));
        // Leaving a text field saves it now. Once nothing is queued for it,
        // it shows the server's cleaned value (trimmed, clamped), which a
        // save that landed while it was focused could not write.
        [[label, 'label'], [scale, 'scale_group'], [missing, 'missing_codes']].forEach(([node, f]) => {
            node.addEventListener('change', () => flush());
            node.addEventListener('blur', () => {
                if (!hasPending(col, f)) syncField(refs, f, false);
            });
        });
        toggle.addEventListener('click', () => setExpanded(refs, toggle.getAttribute('aria-expanded') !== 'true'));
        return refs;
    }

    function hasPending(col, fieldName) {
        return !!((cb.pending[col] && own(cb.pending[col], fieldName))
            || (cb.inflight && cb.inflight[col] && own(cb.inflight[col], fieldName)));
    }

    /** May a refresh overwrite this control? Not while the user is typing in
     *  it or an edit to it is still on its way to the server. */
    function settable(col, fieldName, node, force) {
        if (force) return true;
        if (hasPending(col, fieldName)) return false;
        return document.activeElement !== node;
    }

    function badgeTitle(entry) {
        const parts = [];
        Object.keys(FIELD_NAMES).forEach((f) => {
            const src = entry.field_sources[f];
            if (!src) return;
            parts.push(FIELD_NAMES[f] + ': ' + (src === 'user' ? 'edited by you' : (SOURCE_BADGES[src] || src)));
        });
        return parts.length ? 'Where each field came from. ' + parts.join('; ') + '.'
            : 'Nothing describes this variable yet.';
    }

    function valuesCountText(n) {
        if (!n) return 'Add values';
        return n === 1 ? '1 value' : n + ' values';
    }

    function syncField(refs, fieldName, force) {
        const entry = cb.entries[refs.col];
        if (!entry) return;
        const col = refs.col;
        if (fieldName === 'type' && settable(col, 'type', refs.type, force)) refs.type.value = entry.type || '';
        if (fieldName === 'role' && settable(col, 'role', refs.role, force)) refs.role.value = entry.role;
        if (fieldName === 'label' && settable(col, 'label', refs.label, force)) refs.label.value = entry.label;
        if (fieldName === 'scale_group' && settable(col, 'scale_group', refs.scale, force)) {
            refs.scale.value = entry.scale_group || '';
        }
        if (fieldName === 'missing_codes' && settable(col, 'missing_codes', refs.missing, force)) {
            refs.missing.value = entry.missing_codes.join(', ');
        }
        if (fieldName === 'value_labels') {
            const open = refs.toggle.getAttribute('aria-expanded') === 'true';
            const busy = refs.panel.contains(document.activeElement);
            if (force || (!hasPending(col, 'value_labels') && !busy)) {
                refs.toggleText.textContent = valuesCountText(Object.keys(entry.value_labels).length);
                if (open) buildValues(refs);
                else refs.panel.replaceChildren();     // rebuilt from the entry when opened
            }
        }
    }

    function syncRow(refs, force) {
        const entry = cb.entries[refs.col];
        if (!entry) return;
        ['type', 'label', 'role', 'scale_group', 'missing_codes', 'value_labels']
            .forEach((f) => syncField(refs, f, force));
        refs.badge.textContent = SOURCE_BADGES[entry.source] || entry.source;
        refs.badge.className = 'cb-badge cb-src-' + (own(SOURCE_BADGES, entry.source) ? entry.source : 'default');
        refs.badge.title = badgeTitle(entry);
        Object.keys(refs.fields).forEach((f) => {
            refs.fields[f].classList.toggle('cb-user', entry.user_fields.indexOf(f) !== -1);
        });
        refs.toggle.classList.toggle('cb-user', entry.user_fields.indexOf('value_labels') !== -1);
        refs.li.classList.toggle('cb-unset', entry.source === 'default');
    }

    // --- value-labels editor (built when a row is expanded) ---------------
    function vlRow(refs, code, text) {
        const row = el('div', 'cb-vl-row');
        const codeInput = input('cb-vl-code', refs.col, 'Value code for ', 50, 'Code');
        codeInput.value = code;
        const textInput = input('cb-vl-text', refs.col, 'Meaning of the value for ', 200, 'Meaning');
        textInput.value = text;
        const remove = el('button', 'cb-vl-remove', '×');
        remove.type = 'button';
        remove.setAttribute('aria-label', 'Remove this value label from ' + refs.col);
        remove.title = 'Remove';
        codeInput.addEventListener('input', () => saveValues(refs));
        textInput.addEventListener('input', () => saveValues(refs));
        [codeInput, textInput].forEach((node) => node.addEventListener('change', () => flush()));
        remove.addEventListener('click', () => {
            row.remove();
            saveValues(refs);
            const add = refs.panel.querySelector('.cb-vl-add');
            if (add) add.focus();
        });
        row.append(codeInput, textInput, remove);
        return row;
    }

    function buildValues(refs) {
        const entry = cb.entries[refs.col];
        const list = el('div', 'cb-vl-list');
        sortedCodes(entry.value_labels).forEach((code) => {
            list.appendChild(vlRow(refs, code, entry.value_labels[code]));
        });
        const hint = el('p', 'cb-vl-hint',
            'Codes as they appear in the data, for example 1 or 99. A code without a meaning still counts.');
        const add = el('button', 'wb-btn cb-vl-add', 'Add value label');
        add.type = 'button';
        add.addEventListener('click', () => {
            const row = vlRow(refs, '', '');
            list.appendChild(row);
            row.querySelector('.cb-vl-code').focus();
        });
        refs.panel.replaceChildren(list, add, hint);
    }

    function collectValues(refs) {
        const out = {};
        refs.panel.querySelectorAll('.cb-vl-row').forEach((row) => {
            const code = row.querySelector('.cb-vl-code').value.trim();
            if (code) out[code] = row.querySelector('.cb-vl-text').value.trim();
        });
        return out;
    }

    function saveValues(refs) {
        const values = collectValues(refs);
        refs.toggleText.textContent = valuesCountText(Object.keys(values).length);
        edit(refs.col, 'value_labels', values);
    }

    function setExpanded(refs, open) {
        refs.toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        refs.li.classList.toggle('cb-open', open);
        if (open) {
            if (!refs.panel.firstChild) buildValues(refs);
            refs.panel.classList.remove('hidden');
        } else {
            refs.panel.classList.add('hidden');
        }
    }

    // =====================================================================
    // Rendering the list
    // =====================================================================
    function focusKey() {
        const active = document.activeElement;
        const list = byId('cbList');
        if (!active || !list || !list.contains(active)) return null;
        const li = active.closest('.cb-row');
        return li ? { col: li.dataset.col, cls: active.classList[1] || '' } : null;
    }

    function build() {
        const list = byId('cbList');
        if (!list) return;
        const focus = focusKey();
        cb.rows = new Map();
        const frag = document.createDocumentFragment();
        cb.columns.forEach((col, i) => {
            const refs = buildRow(col, i);
            cb.rows.set(col, refs);
            syncRow(refs, true);
            frag.appendChild(refs.li);
        });
        list.replaceChildren(frag);
        if (focus && focus.cls) {
            const refs = cb.rows.get(focus.col);
            const target = refs && refs.li.querySelector('.' + focus.cls);
            if (target) target.focus();
        }
    }

    function renderChrome() {
        const has = cb.columns.length > 0;
        const empty = byId('cbEmpty');
        const head = byId('cbHeadRow');
        const hint = byId('cbHint');
        if (empty) empty.classList.toggle('hidden', has);
        if (hint) hint.classList.toggle('hidden', !has);
        if (head) head.classList.toggle('hidden', !has);
        ['cbFilter', 'cbImportBtn', 'cbExportBtn'].forEach((id) => {
            const node = byId(id);
            if (node) node.disabled = !has;
        });
        renderSummary();
    }

    function renderSummary() {
        const node = byId('cbSummary');
        if (!node) return;
        if (cb.loadError) { node.textContent = cb.loadError; return; }
        const n = cb.columns.length;
        if (!n) { node.textContent = ''; return; }
        const edited = cb.columns.filter((c) => cb.entries[c] && cb.entries[c].source === 'user').length;
        const unset = cb.columns.filter((c) => cb.entries[c] && cb.entries[c].source === 'default').length;
        const parts = [n === 1 ? '1 variable' : n + ' variables'];
        if (edited) parts.push(edited + ' edited by you');
        if (unset) parts.push(unset + ' not described yet');
        const shown = cb.columns.filter((c) => {
            const r = cb.rows.get(c);
            return r && !r.li.classList.contains('hidden');
        }).length;
        if (shown !== n) parts.push('showing ' + shown);
        if (cb.inflight || Object.keys(cb.pending).length) parts.push('saving');
        node.textContent = parts.join(' · ');
    }

    function applyFilter() {
        const box = byId('cbFilter');
        const q = box ? box.value.trim().toLowerCase() : '';
        cb.rows.forEach((refs, col) => {
            const entry = cb.entries[col];
            const hit = !q || col.toLowerCase().indexOf(q) !== -1
                || (entry && entry.label.toLowerCase().indexOf(q) !== -1);
            refs.li.classList.toggle('hidden', !hit);
        });
        renderSummary();
    }

    /** Keep the sidebar chips in step with the server codebook: the type a
     *  user picked, and the label from whichever source won. */
    function syncChips() {
        if (!CC.renderCodebookUI) return;
        const levels = {};
        const labels = {};
        cb.columns.forEach((c) => {
            const e = cb.entries[c];
            if (!e) return;
            if (e.type) levels[c] = TYPE_LABELS[e.type] || e.type;
            if (e.label) labels[c] = e.label;
        });
        if (!Object.keys(levels).length) return;     // keep what classification showed
        if (same(levels, CC.state.codebook) && same(labels, CC.state.pdfMapping)) return;
        CC.state.codebook = levels;
        CC.state.pdfMapping = labels;
        CC.renderCodebookUI({ keepState: true });
    }

    function adopt(data, opts) {
        const options = data.options || {};
        if (Array.isArray(options.types) && options.types.length) cb.types = options.types.map(String);
        if (Array.isArray(options.roles) && options.roles.length) cb.roles = options.roles.map(String);
        const cols = Array.isArray(data.columns) ? data.columns.map(String) : [];
        const raw = data.codebook && typeof data.codebook === 'object' ? data.codebook : {};
        const sameRows = cb.filename === CC.state.filename && cols.length === cb.columns.length
            && cols.every((c, i) => c === cb.columns[i]) && cb.rows.size === cols.length;
        cb.filename = CC.state.filename;
        cb.loadError = '';
        cb.entries = {};
        cols.forEach((c) => { cb.entries[c] = normalize(raw[c]); });
        // An edit to a column the new version no longer has cannot be saved.
        Object.keys(cb.pending).forEach((c) => { if (!own(cb.entries, c)) delete cb.pending[c]; });
        if (sameRows) {
            const only = opts && opts.only;
            cols.forEach((c) => {
                if (!only || own(only, c)) syncRow(cb.rows.get(c), false);
            });
        } else {
            cb.columns = cols;
            build();
        }
        renderChrome();
        applyFilter();
        syncChips();
    }

    function clear() {
        cb.filename = '';
        cb.columns = [];
        cb.entries = {};
        cb.rows = new Map();
        cb.loadError = '';
        const list = byId('cbList');
        if (list) list.replaceChildren();
        renderChrome();
    }

    // =====================================================================
    // Server sync
    // =====================================================================
    async function refresh() {
        const filename = CC.state.filename;
        const seq = ++cb.seq;
        if (cb.filename && filename !== cb.filename) {
            if (Object.keys(cb.pending).length) flush();     // still saved, to the old dataset
            clear();
        }
        if (!filename) { clear(); return; }
        let res;
        try {
            res = await CC.getJSON('/codebook?filename=' + encodeURIComponent(filename));
        } catch (e) {
            res = null;
        }
        if (seq !== cb.seq || filename !== CC.state.filename) return;
        if (!res || !res.ok) {
            // A background read: say so in place rather than with a toast.
            if (res && res.status === 401) return;
            cb.loadError = res ? CC.errorMessage(res.status, res.data, 'Could not load the codebook')
                : 'Network error: could not load the codebook.';
            renderSummary();
            return;
        }
        adopt(res.data || {});
    }

    function edit(col, fieldName, value, soon) {
        if (!cb.filename || !own(cb.entries, col)) return;
        if (cb.pendingFile && cb.pendingFile !== cb.filename && Object.keys(cb.pending).length) flush();
        cb.pendingFile = cb.filename;
        const queued = cb.pending[col] || {};
        const saved = cb.entries[col][fieldName];
        const inflight = cb.inflight && cb.inflight[col] && own(cb.inflight[col], fieldName);
        if (!inflight && equalField(fieldName, value, saved)) {
            // Back to what the server has: nothing to send for this field.
            delete queued[fieldName];
        } else {
            queued[fieldName] = value;
        }
        if (Object.keys(queued).length) cb.pending[col] = queued;
        else delete cb.pending[col];
        clearTimeout(cb.timer);
        if (Object.keys(cb.pending).length) cb.timer = setTimeout(flush, soon ? 150 : SAVE_DELAY);
        renderSummary();
    }

    function saveError(res) {
        if (!res) return 'Network error: the codebook edit was not saved.';
        if (res.status === 404) {
            return 'This dataset is no longer on the server, so the edit was not saved. '
                + 'Upload it again to keep editing its codebook.';
        }
        return CC.errorMessage(res.status, res.data, 'Could not save the codebook edit');
    }

    /** Put the fields of a failed batch back to the server's values. */
    function revert(batch) {
        Object.keys(batch).forEach((col) => {
            const refs = cb.rows.get(col);
            if (!refs) return;
            Object.keys(batch[col]).forEach((f) => {
                if (cb.pending[col] && own(cb.pending[col], f)) return;   // a newer edit is queued
                syncField(refs, f, true);
            });
        });
    }

    async function save(filename, batch) {
        let res;
        try { res = await CC.put('/codebook', { filename, codebook: batch }); } catch (e) { res = null; }
        cb.inflight = null;
        const current = filename === cb.filename && filename === CC.state.filename;
        if (res && res.ok && res.data && Array.isArray(res.data.columns)) {
            if (current) {
                // This answer carries the whole codebook after the edit: a
                // refresh read sent before it would put the old values back.
                cb.seq += 1;
                adopt(res.data, { only: batch });
            }
            return;
        }
        if (current) revert(batch);
        CC.toast(saveError(res), 'error');
    }

    function flush() {
        clearTimeout(cb.timer);
        cb.timer = null;
        if (cb.inflightPromise) { cb.again = true; return cb.inflightPromise; }
        if (!Object.keys(cb.pending).length) return Promise.resolve();
        const batch = cb.pending;
        const filename = cb.pendingFile;
        cb.pending = {};
        cb.inflight = batch;
        renderSummary();
        cb.inflightPromise = save(filename, batch).finally(() => {
            cb.inflight = null;
            cb.inflightPromise = null;
            renderSummary();
            if (cb.again) { cb.again = false; flush(); }
        });
        return cb.inflightPromise;
    }

    /** Every queued edit saved (or failed) before an import or export. */
    async function settle() {
        for (let i = 0; i < 5; i += 1) {
            if (cb.inflightPromise) { await cb.inflightPromise; continue; }
            if (Object.keys(cb.pending).length) { await flush(); continue; }
            return;
        }
    }

    /** Leaving the page with an edit still queued: send it anyway. */
    function flushOnExit() {
        if (!Object.keys(cb.pending).length || !cb.pendingFile) return;
        const body = JSON.stringify({ filename: cb.pendingFile, codebook: cb.pending });
        cb.pending = {};
        try {
            fetch('/codebook', { method: 'PUT', headers: CC.headers(), body, keepalive: true });
        } catch (e) { /* the page is going away */ }
    }

    // =====================================================================
    // Import / export
    // =====================================================================
    async function importFile(file) {
        if (!file) return;
        if (!cb.filename) { CC.toast('Upload a dataset first, then import its codebook.', 'info'); return; }
        if (file.size > IMPORT_MAX_BYTES) {
            CC.toast(file.name + ' is too large to be a codebook (5 MB at most).', 'error');
            return;
        }
        let doc;
        try {
            doc = JSON.parse(await file.text());
        } catch (e) {
            CC.toast(file.name + ' is not valid JSON, so nothing was imported.', 'error');
            return;
        }
        await settle();
        const filename = cb.filename;
        const btn = byId('cbImportBtn');
        if (btn) btn.disabled = true;
        let res;
        try { res = await CC.post('/codebook/import', { filename, codebook: doc }); } catch (e) { res = null; }
        if (btn) btn.disabled = !cb.columns.length;
        if (!res || !res.ok) {
            CC.toast(res ? CC.errorMessage(res.status, res.data, 'Could not import the codebook')
                : 'Network error: the codebook was not imported.', 'error');
            return;
        }
        if (filename !== CC.state.filename) return;
        const data = res.data || {};
        cb.seq += 1;                 // discard any older refresh read (see save)
        adopt(data);
        const n = typeof data.imported === 'number' ? data.imported : 0;
        const ignored = Array.isArray(data.ignored) ? data.ignored.map(String) : [];
        let msg = n ? 'Imported edits for ' + (n === 1 ? '1 variable.' : n + ' variables.')
            : 'Nothing to import: the file has no edited fields for this dataset\'s variables.';
        if (ignored.length) {
            const shown = ignored.slice(0, 5).join(', ');
            msg += ' Skipped ' + ignored.length + ' not in this dataset: ' + shown
                + (ignored.length > 5 ? ', and more.' : '.');
        }
        CC.toast(msg, n ? 'success' : 'info');
        CC.announce(msg);
    }

    async function exportJSON() {
        if (!cb.filename) { CC.toast('Upload a dataset first.', 'info'); return; }
        await settle();
        const btn = byId('cbExportBtn');
        if (btn) btn.disabled = true;
        try {
            const res = await fetch('/codebook/export?filename=' + encodeURIComponent(cb.filename),
                { headers: { 'X-CSRF-Token': CC.csrfToken } });
            if (!res.ok) {
                const data = await CC.readJSON(res);
                CC.toast(CC.errorMessage(res.status, data, 'Could not export the codebook'), 'error');
                return;
            }
            const blob = await res.blob();
            const disposition = res.headers.get('Content-Disposition') || '';
            const named = /filename="?([^";]+)"?/i.exec(disposition);
            const stem = cb.filename.replace(/\.[^.]+$/, '');
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = named ? named[1] : stem + '_codebook.json';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 60000);
        } catch (e) {
            CC.toast('Network error: the codebook was not exported.', 'error');
        } finally {
            if (btn) btn.disabled = !cb.columns.length;
        }
    }

    // =====================================================================
    // Views: Variables (this editor) and Document (data.js's viewer)
    // =====================================================================
    function show(view) {
        const doc = view === 'document';
        const editor = byId('cbEditor');
        const document_ = byId('cbDocument');
        if (editor) editor.classList.toggle('hidden', doc);
        if (document_) document_.classList.toggle('hidden', !doc);
        const v = byId('cbViewVariables');
        const d = byId('cbViewDocument');
        if (v) v.setAttribute('aria-pressed', doc ? 'false' : 'true');
        if (d) d.setAttribute('aria-pressed', doc ? 'true' : 'false');
    }

    CC.codebookEditor = {
        refresh,
        show,
    };

    document.addEventListener('DOMContentLoaded', () => {
        const v = byId('cbViewVariables');
        const d = byId('cbViewDocument');
        if (v) v.addEventListener('click', () => show('variables'));
        if (d) d.addEventListener('click', () => show('document'));
        const filter = byId('cbFilter');
        if (filter) {
            filter.addEventListener('input', () => {
                clearTimeout(cb.filterTimer);
                cb.filterTimer = setTimeout(applyFilter, FILTER_DELAY);
            });
        }
        const importBtn = byId('cbImportBtn');
        const importInput = byId('cbImportInput');
        if (importBtn && importInput) importBtn.addEventListener('click', () => importInput.click());
        if (importInput) {
            importInput.addEventListener('change', async () => {
                const file = importInput.files[0];
                importInput.value = '';       // the same file can be picked again
                await importFile(file);
            });
        }
        const exportBtn = byId('cbExportBtn');
        if (exportBtn) exportBtn.addEventListener('click', exportJSON);
        window.addEventListener('pagehide', flushOnExit);
        show('variables');
        renderChrome();
    });
}());
