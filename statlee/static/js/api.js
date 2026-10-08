/* STATlee core namespace: state, fetch/CSRF, SSE, sanitized markdown,
 * usage accounting, console-error ring buffer (for issue reports). */
(function () {
    'use strict';

    const csrfMeta = document.querySelector('meta[name="csrf-token"]');

    window.CC = {
        csrfToken: csrfMeta ? csrfMeta.content : '',
        state: {
            filename: '',
            headers: [],
            codebook: {},
            pdfMapping: {},
            chatHistory: [],
            converseHistory: [],
            latestInterpretation: '',
            lastRun: null,          // {output, plots, success}
            lastPrompt: '',
            suggestions: [],
            report: '',
            user: null,
            currentPage: 1,
            totalPages: 1,
            usage: { input: 0, output: 0, calls: 0, by_model: {} },
        },
        // Per-model price estimates (USD per 1M tokens) injected by the server
        // for the session-cost display. Display only: never triggers spend.
        prices: (window.CC_BOOT && window.CC_BOOT.prices) || {},
        consoleErrors: [],
    };

    // --- console error ring buffer (6.3) ---------------------------------
    window.addEventListener('error', (e) => {
        CC.consoleErrors.push(`${new Date().toISOString()} ${e.message} @ ${e.filename}:${e.lineno}`);
        if (CC.consoleErrors.length > 20) CC.consoleErrors.shift();
    });
    window.addEventListener('unhandledrejection', (e) => {
        CC.consoleErrors.push(`${new Date().toISOString()} unhandled rejection: ${e.reason}`);
        if (CC.consoleErrors.length > 20) CC.consoleErrors.shift();
    });

    // --- networking --------------------------------------------------------
    CC.headers = function (extra) {
        return Object.assign(
            { 'Content-Type': 'application/json', 'X-CSRF-Token': CC.csrfToken },
            extra || {});
    };

    /** POST JSON, parse JSON. Returns {ok, status, data}. */
    CC.post = async function (url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: CC.headers(),
            body: JSON.stringify(body || {}),
        });
        let data = {};
        try { data = await res.json(); } catch (e) { /* non-JSON body */ }
        return { ok: res.ok, status: res.status, data };
    };

    /** POST multipart form (file uploads) with the CSRF header. */
    CC.postForm = async function (url, formData) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'X-CSRF-Token': CC.csrfToken },
            body: formData,
        });
        let data = {};
        try { data = await res.json(); } catch (e) { /* non-JSON body */ }
        return { ok: res.ok, status: res.status, data };
    };

    /** POST expecting an SSE stream; returns the raw Response. */
    CC.postStream = function (url, body) {
        return fetch(url, {
            method: 'POST',
            headers: CC.headers(),
            body: JSON.stringify(body || {}),
        });
    };

    /** Read a non-streaming JSON body without throwing. A proxy's 502/504 HTML
     *  page is not JSON; callers get {} and fall back to the HTTP status
     *  instead of mislabelling a server error as a "network error" (D4-22). */
    CC.readJSON = async function (response) {
        try { return await response.json(); } catch (e) { return {}; }
    };

    /** Human message for a non-streaming error reply. */
    CC.errorMessage = function (status, data, fallback) {
        if (data && data.error) return CC.explainError(data.error);
        if (status) return (fallback || 'Request failed') + ' (HTTP ' + status + ').';
        return fallback || 'Request failed.';
    };

    /** Map server wording the user cannot act on to wording they can. The
     *  anonymous TTL reaper deletes the dataset; every route then answers
     *  "Invalid filename" (D4-20). */
    CC.explainError = function (msg) {
        if (/^invalid filename/i.test(String(msg || '')) && CC.state.filename && !CC.state.user) {
            return 'Your anonymous session data has expired and was deleted. Upload the dataset again to continue.';
        }
        return msg;
    };

    /** Parse an SSE response. Handlers: onDelta, onPhase, onDone, onError.
     *  onPhase receives (phase, message): some phases are notices rather than
     *  progress (P2-8's feature-selection fallback carries the text to show),
     *  so the message must reach the caller instead of being dropped here.
     *
     *  Every stream ends in exactly one terminal callback. If the connection
     *  closes with neither a `done` nor an `error` event (proxy idle timeout,
     *  worker restart, a mobile network handoff), onError fires with a
     *  "connection closed" message, so callers have one failure path (D4-7).
     *  Resolves to {terminated: true|false}. */
    CC.streamSSE = async function (response, handlers) {
        const h = handlers || {};
        let terminal = false;
        const dispatch = (parsed) => {
            if (parsed.type === 'delta' && h.onDelta) h.onDelta(parsed.text || '');
            else if (parsed.type === 'phase' && h.onPhase) h.onPhase(parsed.phase, parsed.message);
            else if (parsed.type === 'done') { terminal = true; if (h.onDone) h.onDone(parsed); }
            else if (parsed.type === 'error') {
                terminal = true;
                if (h.onError) h.onError(CC.explainError(parsed.message || 'Stream error.'));
            }
        };
        const handleEvent = (event) => {
            if (!event.trim()) return;
            const dataLine = event.split('\n').find(l => l.startsWith('data: '));
            if (!dataLine) return;
            let parsed;
            try { parsed = JSON.parse(dataLine.slice(6)); }
            catch (e) { console.error('Bad SSE payload:', dataLine, e); return; }
            dispatch(parsed);
        };
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        try {
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const events = buffer.split('\n\n');
                buffer = events.pop();
                for (const event of events) handleEvent(event);
            }
            if (buffer) handleEvent(buffer);   // a final event with no trailing blank line
        } catch (e) {
            if (!terminal && h.onError) h.onError('Connection lost while the response was streaming.');
            return { terminated: false };
        }
        if (!terminal) {
            if (h.onError) h.onError('Connection closed before the response finished.');
            return { terminated: false };
        }
        return { terminated: true };
    };

    // --- rendering helpers ---------------------------------------------------
    CC.escapeHtml = function (value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    };

    /** Sanitized markdown rendering: the only path LLM text takes into the
     *  DOM (roadmap 0.5).
     *
     *  Model text can carry prompt-injected HTML (dataset cells and column
     *  names reach the prompts, and terminal output is fed to /interpret), so
     *  DOMPurify's defaults are not enough (D4-4): they keep data-* attributes
     *  (which boot.js's click dispatcher reads), inline style (a full-screen
     *  click-jacking overlay) and form controls (a fake "session expired"
     *  form). id/name/class are dropped too: no DOM clobbering of the app's own
     *  ids, and no borrowing the app's button styles. Links open in a new tab,
     *  so a click on one never throws away the in-memory session. */
    const SANITIZE_CONFIG = {
        ALLOW_DATA_ATTR: false,
        ALLOW_ARIA_ATTR: false,
        FORBID_ATTR: ['style', 'id', 'name', 'class', 'form', 'formaction', 'action',
            'tabindex', 'autofocus', 'target', 'srcset', 'ping'],
        FORBID_TAGS: ['style', 'form', 'button', 'input', 'textarea', 'select', 'option',
            'optgroup', 'label', 'fieldset', 'iframe', 'frame', 'frameset', 'object', 'embed',
            'video', 'audio', 'source', 'track', 'dialog', 'details', 'summary', 'svg', 'math',
            'base', 'link', 'meta'],
    };
    let sanitizeHooked = false;
    function hookSanitizer() {
        if (sanitizeHooked || !window.DOMPurify || !DOMPurify.addHook) return;
        sanitizeHooked = true;
        DOMPurify.addHook('afterSanitizeAttributes', (node) => {
            if (node.tagName === 'A' && node.hasAttribute('href')) {
                node.setAttribute('target', '_blank');
                node.setAttribute('rel', 'noopener noreferrer');
            }
        });
    }
    CC.renderMarkdown = function (md) {
        hookSanitizer();
        const html = marked.parse(md || '');
        return DOMPurify.sanitize(html, SANITIZE_CONFIG);
    };

    /** One polite screen-reader announcement (D4-14). Streamed output is not a
     *  live region itself (every chunk would be read out); instead the stream's
     *  end is announced once here. */
    CC.announce = function (message) {
        const el = document.getElementById('srAnnounce');
        if (!el) return;
        el.textContent = '';
        setTimeout(() => { el.textContent = message; }, 60);
    };

    CC.spinner = function (cls) {
        return '<svg class="animate-spin ' + (cls || 'h-4 w-4') + '" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">' +
            '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
            '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>';
    };

    CC.debounce = function (fn, ms) {
        let t;
        return function () {
            clearTimeout(t);
            const args = arguments;
            t = setTimeout(() => fn.apply(null, args), ms);
        };
    };

    // --- usage accounting (3.4 cost display) -----------------------------------
    CC.fmtUSD = function (usd) {
        if (!usd || usd <= 0) return '$0.00';
        if (usd < 0.01) return '<$0.01';
        return '$' + usd.toFixed(usd < 1 ? 3 : 2);
    };

    /** Approximate session cost in USD from the per-model token tally and the
     *  injected price map. Models without a known price contribute nothing. */
    CC.sessionCostUSD = function () {
        const by = CC.state.usage.by_model || {};
        let usd = 0;
        Object.keys(by).forEach((model) => {
            const p = CC.prices[model];
            if (!p) return;
            usd += (by[model].input / 1e6) * (p.input || 0)
                 + (by[model].output / 1e6) * (p.output || 0);
        });
        return usd;
    };

    CC.addUsage = function (usage) {
        if (!usage) return;
        CC.state.usage.input += usage.input || 0;
        CC.state.usage.output += usage.output || 0;
        CC.state.usage.calls += usage.calls || 1;
        // Merge the per-model split so the tooltip can price each model.
        const incoming = usage.by_model || {};
        const by = CC.state.usage.by_model;
        Object.keys(incoming).forEach((model) => {
            const acc = by[model] || (by[model] = { input: 0, output: 0, calls: 0 });
            acc.input += incoming[model].input || 0;
            acc.output += incoming[model].output || 0;
            acc.calls += incoming[model].calls || 0;
        });
        const badge = document.getElementById('usageBadge');
        if (!badge) return;
        const total = CC.state.usage.input + CC.state.usage.output;
        const cost = CC.sessionCostUSD();
        badge.textContent = `${(total / 1000).toFixed(1)}k tok · ${CC.state.usage.calls} calls`;
        // Hover tooltip: totals + approximate session cost + per-model lines.
        const lines = [
            'Session LLM usage',
            `Input ${CC.state.usage.input.toLocaleString()} · Output ${CC.state.usage.output.toLocaleString()} tokens · ${CC.state.usage.calls} calls`,
            `Estimated cost ≈ ${CC.fmtUSD(cost)}  (provider list prices; display only)`,
        ];
        const models = Object.keys(by);
        if (models.length) {
            lines.push('');
            models.forEach((model) => {
                const m = by[model];
                const p = CC.prices[model];
                const c = p ? (m.input / 1e6) * (p.input || 0) + (m.output / 1e6) * (p.output || 0) : 0;
                lines.push(`${model}: ${m.input.toLocaleString()}/${m.output.toLocaleString()} in/out · ${p ? '≈ ' + CC.fmtUSD(c) : 'no price'}`);
            });
        }
        badge.title = lines.join('\n');
        badge.classList.remove('hidden');
    };

    // --- toasts ------------------------------------------------------------------
    /** Errors are announced assertively, stay long enough to read (a 300-char
     *  /wrangle error does not fit in 4 s), and can be dismissed (D4-14). */
    CC.toast = function (message, type) {
        const area = document.getElementById('toastArea');
        if (!area) { console.log('[toast]', message); return; }
        const kind = type || 'info';
        const el = document.createElement('div');
        el.className = 'cc-toast ' + kind;
        el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
        const text = document.createElement('span');
        text.className = 'cc-toast-text';
        text.textContent = message;
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'cc-toast-close';
        close.setAttribute('aria-label', 'Dismiss notification');
        close.textContent = '×';
        el.appendChild(text);
        el.appendChild(close);
        area.appendChild(el);
        let timer = null;
        const dismiss = () => {
            clearTimeout(timer);
            el.style.opacity = '0'; el.style.transition = 'opacity .4s';
            setTimeout(() => el.remove(), 400);
        };
        const life = kind === 'error' ? 12000 : 5000;
        const arm = () => { clearTimeout(timer); timer = setTimeout(dismiss, life); };
        close.addEventListener('click', dismiss);
        el.addEventListener('mouseenter', () => clearTimeout(timer));   // hold while reading
        el.addEventListener('mouseleave', arm);
        arm();
    };
}());
