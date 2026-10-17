/* Methods and assumptions card (SP3): the run's parsed methods block, shown
 * in Results above the terminal.
 *
 * The card comes from the server (/run and GET /runs/<id> return `methods`,
 * parsed from the run's own output by statlee/methods.py). Its values are
 * text a model-written script printed, so this module builds the card with
 * createElement + textContent only. Verdict chips carry a text label (PASS,
 * WARN, FAIL), never color alone. */
(function () {
    'use strict';

    const CHIPS = {
        pass: { label: 'PASS', title: 'Check passed' },
        warn: { label: 'WARN', title: 'Warning: check with care' },
        fail: { label: 'FAIL', title: 'Check failed' },
        unknown: { label: 'NOTE', title: 'No verdict reported' },
    };
    const NOT_REPORTED = 'Assumptions not reported';

    function byId(id) { return document.getElementById(id); }

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function str(value) {
        return typeof value === 'string' ? value : '';
    }

    function isCount(value) {
        return typeof value === 'number' && Number.isInteger(value) && value >= 0;
    }

    function field(list, label, value) {
        if (value === '' || value === null || value === undefined) return;
        const item = el('div', 'mc-field');
        item.appendChild(el('dt', null, label));
        item.appendChild(el('dd', null, value));
        list.appendChild(item);
    }

    function methodBlock(method) {
        if (!method || typeof method !== 'object') {
            return el('p', 'mc-empty-sub', 'Method not reported.');
        }
        const list = el('dl', 'mc-method');
        field(list, 'Test', str(method.test));
        field(list, 'Outcome', str(method.outcome));
        field(list, 'Predictors', str(method.predictors));
        if (isCount(method.n_used)) field(list, 'N used', method.n_used.toLocaleString());
        if (isCount(method.dropped)) {
            const reason = str(method.dropped_reason);
            field(list, 'Dropped', method.dropped.toLocaleString() + (reason ? ' (' + reason + ')' : ''));
        }
        return list.childNodes.length ? list : el('p', 'mc-empty-sub', 'Method not reported.');
    }

    function chip(status) {
        const key = Object.prototype.hasOwnProperty.call(CHIPS, status) ? status : 'unknown';
        const node = el('span', 'mc-chip ' + key, CHIPS[key].label);
        node.title = CHIPS[key].title;
        return node;
    }

    function checksBlock(assumptions) {
        const wrap = el('div', 'mc-section');
        wrap.appendChild(el('h3', 'mc-sub', 'Assumption checks'));
        if (!assumptions.length) {
            wrap.appendChild(el('p', 'mc-empty-sub', NOT_REPORTED + '.'));
            return wrap;
        }
        const list = el('ul', 'mc-checks');
        assumptions.forEach((a) => {
            if (!a || typeof a !== 'object') return;
            const item = el('li', 'mc-check');
            item.appendChild(chip(a.status));
            const text = el('span', 'mc-check-text');
            text.appendChild(el('span', 'mc-check-name', str(a.name)));
            if (str(a.detail)) text.appendChild(el('span', 'mc-check-detail', str(a.detail)));
            item.appendChild(text);
            list.appendChild(item);
        });
        wrap.appendChild(list);
        return wrap;
    }

    function effectsBlock(effects) {
        const wrap = el('div', 'mc-section');
        wrap.appendChild(el('h3', 'mc-sub', 'Effect sizes'));
        if (!effects.length) {
            wrap.appendChild(el('p', 'mc-empty-sub', 'No effect sizes reported.'));
            return wrap;
        }
        const list = el('ul', 'mc-effects');
        effects.forEach((e) => {
            if (e && typeof e === 'object' && str(e.text)) list.appendChild(el('li', null, e.text));
        });
        wrap.appendChild(list);
        return wrap;
    }

    function summary(assumptions) {
        const counts = { pass: 0, warn: 0, fail: 0 };
        assumptions.forEach((a) => {
            if (a && Object.prototype.hasOwnProperty.call(counts, a.status)) counts[a.status] += 1;
        });
        const parts = [];
        if (counts.pass) parts.push(counts.pass + ' pass');
        if (counts.warn) parts.push(counts.warn + ' warn');
        if (counts.fail) parts.push(counts.fail + ' fail');
        return parts.join(' · ');
    }

    /** Hide the card: no run, a run that never executed, a new dataset. */
    CC.clearMethods = function () {
        const card = byId('methodsCard');
        const body = byId('methodsCardBody');
        const sum = byId('methodsCardSummary');
        if (body) body.replaceChildren();
        if (sum) sum.textContent = '';
        if (card) {
            card.classList.add('hidden');
            card.classList.remove('mc-missing');
        }
    };

    /** Show a run's card. `methods` is the server's parsed card, or
     *  null/undefined when the reply carried none (the card hides). A failed
     *  run without a block hides it too: the failure is the story there.
     *  `opts.failed` marks a failed run. */
    CC.renderMethods = function (methods, opts) {
        CC.clearMethods();
        const card = byId('methodsCard');
        const body = byId('methodsCardBody');
        const sum = byId('methodsCardSummary');
        if (!card || !body || !methods || typeof methods !== 'object') return;
        const o = opts || {};
        const assumptions = Array.isArray(methods.assumptions) ? methods.assumptions : [];
        const effects = Array.isArray(methods.effects) ? methods.effects : [];
        const reported = !!methods.reported;
        if (!reported && o.failed) return;
        if (!reported) {
            card.classList.add('mc-missing');
            body.appendChild(el('p', 'mc-empty', NOT_REPORTED));
            body.appendChild(el('p', 'mc-empty-sub',
                'This script did not print a methods summary, so its test, '
                + 'assumption checks and effect sizes are not listed here. '
                + 'The terminal output below shows everything it printed.'));
            if (sum) sum.textContent = 'not reported';
        } else {
            body.appendChild(methodBlock(methods.method));
            const grid = el('div', 'mc-grid');
            grid.appendChild(checksBlock(assumptions));
            grid.appendChild(effectsBlock(effects));
            body.appendChild(grid);
            if (sum) sum.textContent = summary(assumptions);
        }
        card.classList.remove('hidden');
    };
}());
