/* Statly core namespace: state, fetch/CSRF, SSE, sanitized markdown,
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
            usage: { input: 0, output: 0, calls: 0 },
        },
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

    /** Parse an SSE response. Handlers: onDelta, onPhase, onDone, onError. */
    CC.streamSSE = async function (response, handlers) {
        const h = handlers || {};
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const events = buffer.split('\n\n');
            buffer = events.pop();
            for (const event of events) {
                if (!event.trim()) continue;
                const dataLine = event.split('\n').find(l => l.startsWith('data: '));
                if (!dataLine) continue;
                let parsed;
                try { parsed = JSON.parse(dataLine.slice(6)); }
                catch (e) { console.error('Bad SSE payload:', dataLine, e); continue; }
                if (parsed.type === 'delta' && h.onDelta) h.onDelta(parsed.text || '');
                else if (parsed.type === 'phase' && h.onPhase) h.onPhase(parsed.phase);
                else if (parsed.type === 'done' && h.onDone) h.onDone(parsed);
                else if (parsed.type === 'error' && h.onError) h.onError(parsed.message || 'Stream error.');
            }
        }
    };

    // --- rendering helpers ---------------------------------------------------
    CC.escapeHtml = function (value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    };

    /** Sanitized markdown rendering — the only path LLM text takes into the
     *  DOM (roadmap 0.5). */
    CC.renderMarkdown = function (md) {
        const html = marked.parse(md || '');
        return DOMPurify.sanitize(html);
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
    CC.addUsage = function (usage) {
        if (!usage) return;
        CC.state.usage.input += usage.input || 0;
        CC.state.usage.output += usage.output || 0;
        CC.state.usage.calls += usage.calls || 1;
        const badge = document.getElementById('usageBadge');
        if (badge) {
            const total = CC.state.usage.input + CC.state.usage.output;
            badge.textContent = `${(total / 1000).toFixed(1)}k tok · ${CC.state.usage.calls} calls`;
            badge.title = `Session LLM usage — input: ${CC.state.usage.input.toLocaleString()} tokens, ` +
                `output: ${CC.state.usage.output.toLocaleString()} tokens, calls: ${CC.state.usage.calls}`;
            badge.classList.remove('hidden');
        }
    };

    // --- toasts ------------------------------------------------------------------
    CC.toast = function (message, type) {
        const area = document.getElementById('toastArea');
        if (!area) { console.log('[toast]', message); return; }
        const el = document.createElement('div');
        el.className = 'cc-toast ' + (type || 'info');
        el.setAttribute('role', 'status');
        el.textContent = message;
        area.appendChild(el);
        setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .4s'; }, 4200);
        setTimeout(() => el.remove(), 4700);
    };
}());
