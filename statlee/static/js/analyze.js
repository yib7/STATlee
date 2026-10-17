/* Analysis pipeline UI: editable CodeMirror editor, phase-labelled
 * streaming generation, refinement mode, guarded execution,
 * multi-plot gallery, streamed interpretation incl. auto-debug,
 * and history capture. */
(function () {
    'use strict';

    // "Pro mode" toggle: when on, the backend runs code generation on the
    // configured provider's bigger 'pro_max' model (CC_BOOT.models.pro_max)
    // instead of the default draft model. Read once per request.
    CC.proOn = function () {
        const el = document.getElementById('proToggle');
        return !!(el && el.checked);
    };

    CC.getCode = function () { return CC.editor ? CC.editor.getValue() : ''; };
    CC.setCode = function (text) {
        if (CC.editor) {
            CC.editor.setValue(text || '');
            CC.editor.refresh();
        }
    };

    function languageSelect() { return document.getElementById('languageSelect'); }
    function byId(id) { return document.getElementById(id); }

    // =====================================================================
    // Run-lifecycle chrome (generating / executing / failed)
    // =====================================================================

    /** m:ss elapsed clock driven off a single interval. */
    function makeTimer(valueId) {
        let iv = null;
        function fmt(ms) {
            const s = Math.max(0, Math.floor(ms / 1000));
            return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
        }
        return {
            start() {
                this.stop();
                const t0 = Date.now();
                const paint = () => {
                    const el = byId(valueId);
                    if (el) el.textContent = fmt(Date.now() - t0);
                };
                paint();
                iv = setInterval(paint, 1000);
            },
            stop() { if (iv) { clearInterval(iv); iv = null; } },
        };
    }

    const genTimer = makeTimer('genElapsed');
    const execTimer = makeTimer('execElapsedVal');

    /** The model id that will actually generate this script. Pro mode routes
     *  code generation to the bigger 'pro_max' model (see /chat). Display only. */
    function modelForRun() {
        const models = (window.CC_BOOT && window.CC_BOOT.models) || {};
        return (CC.proOn() ? models.pro_max : models.draft) || '';
    }

    function showModelChip() {
        const chip = byId('genModelChip');
        const name = byId('genModelName');
        const badge = byId('genProBadge');
        if (!chip || !name) return;
        const model = modelForRun();
        if (!model) { chip.classList.add('hidden'); return; }
        name.textContent = model;
        chip.classList.remove('hidden');
        if (badge) badge.classList.toggle('hidden', !CC.proOn());
    }

    /** The streaming editor chrome: the "streaming" pip in the title bar and the
     *  "Auto-scrolling with output" footer that explains why the view moves. */
    function setStreaming(on) {
        const badge = byId('streamState');
        const note = byId('autoScrollNote');
        if (badge) badge.classList.toggle('hidden', !on);
        if (note) note.classList.toggle('hidden', !on);
    }

    function setGenPhase(label) {
        const header = byId('genHeader');
        const phase = byId('genPhase');
        if (!header || !phase) return;
        if (!label) {
            header.classList.add('hidden');
            genTimer.stop();
            setStreaming(false);
            return;
        }
        phase.innerHTML = CC.spinner('h-3.5 w-3.5') +
            '<span>' + CC.escapeHtml(label) +
            '<span class="animate-pulse">…</span></span>';
        if (header.classList.contains('hidden')) {
            // First phase of this generation: reveal the header and start the
            // clock once, so it spans drafting -> validating rather than resetting.
            header.classList.remove('hidden');
            showModelChip();
            genTimer.start();
        }
    }

    // --- failure parsing ---------------------------------------------------
    // Kept deliberately narrow: these only drive presentation (which line is
    // red, which token gets the wavy underline, what the pipeline note says).
    // A miss degrades to plain text, never to a wrong claim.
    const ERR_LINE = /^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt))(?::|$)/;
    const MAX_STYLED_LINES = 400;

    /** The exception line, e.g. "NameError: name 'incom_k' is not defined". */
    function errorSummary(output) {
        const lines = (output || '').trim().split('\n');
        for (let i = lines.length - 1; i >= 0; i--) {
            const line = lines[i].trim();
            if (ERR_LINE.test(line)) return line;
        }
        return lines[lines.length - 1] || 'The script did not finish.';
    }

    /** The token the interpreter actually complained about, if it named one. */
    function culpritFrom(output) {
        const text = output || '';
        const m = /name '([^']+)' is not defined/.exec(text)
            || /KeyError: '([^']+)'/.exec(text)
            || /column[s]? (?:named )?'([^']+)'/i.exec(text)
            || /has no attribute '([^']+)'/.exec(text);
        return m ? m[1] : null;
    }

    /** Where the user's own script blew up. Prefers the deepest frame inside
     *  the file the sandbox actually ran (script.py / script.R, see
     *  sandbox.py): a KeyError raised four frames deep in pandas must point at
     *  the user's line, not at pandas/core/frame.py, which they cannot edit.
     *  Falls back to the deepest frame when the script is not named. */
    function errorLocation(output) {
        const re = /File "([^"]+)", line (\d+)/g;
        let last = null, own = null, hit;
        while ((hit = re.exec(output || '')) !== null) {
            last = hit;
            if (/(^|[\\/])script\.(py|r)$/i.test(hit[1])) own = hit;
        }
        const m = own || last;
        return m ? { file: m[1], line: Number(m[2]) } : null;
    }

    /** Short pipeline note, e.g. "NameError · line 10" (design parity). */
    function failureNote(output) {
        const name = (errorSummary(output).split(':')[0] || 'Failed').trim();
        const loc = errorLocation(output);
        return loc ? name + ' · line ' + loc.line : name;
    }

    /** Wrap every occurrence of `culprit` in an already-escaped line. String
     *  splitting, not regex, so a token containing metacharacters is literal.
     *  Both inputs are escaped first: the only raw HTML here is our own span. */
    function markCulprit(safeLine, culprit) {
        const tok = CC.escapeHtml(culprit);
        if (!tok || safeLine.indexOf(tok) === -1) return safeLine;
        return safeLine.split(tok).join('<span class="lc-culprit">' + tok + '</span>');
    }

    /** Terminal body. Success stays plain text: a 256KB stdout must not become
     *  thousands of DOM nodes. Failure gets the traceback marked up so the eye
     *  lands on the thing to fix. */
    function renderTerminal(output, failed) {
        const el = byId('runOutput');
        if (!el) return;
        const text = output || '';
        const lines = text.split('\n');
        if (!failed || !text.trim() || lines.length > MAX_STYLED_LINES) {
            el.innerText = text.trim() ? text
                : (failed ? 'The script produced no output before it failed.'
                    : 'Execution successful.');
            return;
        }
        const culprit = culpritFrom(text);
        el.innerHTML = lines.map((line) => {
            let safe = CC.escapeHtml(line);
            if (culprit) safe = markCulprit(safe, culprit);
            let cls = '';
            if (/^Traceback \(most recent call last\)/.test(line)) cls = 'lc-trace-err';
            else if (ERR_LINE.test(line)) cls = 'lc-trace-err';
            else if (/^\s/.test(line)) cls = 'lc-trace-dim';
            return cls ? '<span class="' + cls + '">' + safe + '</span>' : safe;
        }).join('\n');
    }

    // --- execution chrome ---------------------------------------------------
    function setTermState(cls, text, blink) {
        const el = byId('termState');
        if (!el) return;
        if (!cls) { el.classList.add('hidden'); return; }
        el.className = 'lc-term-state ' + cls;
        el.innerHTML = '<span class="lc-dot' + (blink ? ' blink' : '') + '"></span>' +
            CC.escapeHtml(text);
    }

    function execRunning() {
        // A run is underway: the terminal and interpretation now carry live
        // content, so the Results empty state and the Report lock both retire.
        document.body.classList.remove('cc-no-run');
        const header = byId('execHeader');
        if (header) header.className = 'lc-header';      // reveal + clear any failed state
        const badge = byId('execBadge');
        const glyph = byId('execFailGlyph');
        if (badge) badge.classList.remove('hidden');
        if (glyph) glyph.classList.add('hidden');
        const label = byId('execPhaseLabel');
        if (label) label.textContent = 'Executing in sandbox';
        const elapsed = byId('execElapsed');
        if (elapsed) elapsed.className = 'lc-elapsed live';
        const dot = elapsed && elapsed.querySelector('.lc-dot');
        if (dot) dot.classList.add('blink');
        const prog = byId('execProgress');
        if (prog) prog.classList.remove('hidden');
        const frame = byId('debugFrame');
        if (frame) frame.classList.add('hidden');
        const icon = byId('termIcon');
        if (icon) icon.classList.remove('failed');
        setTermState('running', 'running', false);
        execTimer.start();
        // No isolation claim here. The default subprocess sandbox has
        // time and memory limits but shares the host network; only docker
        // mode removes it (README "Security").
        const docker = window.CC_BOOT && window.CC_BOOT.sandbox_mode === 'docker';
        CC.pipeline.set('execute', 'active',
            docker ? 'network-isolated container' : 'running on the server');
    }

    /** `returncode` may be null when the request never reached the sandbox
     *  (run-guard refusal, timeout, network): then we claim no exit code, and
     *  `opts.noFix` drops "Fix with AI" since there is no traceback. */
    function execFinished(failed, returncode, note, opts) {
        execTimer.stop();
        const prog = byId('execProgress');
        if (prog) prog.classList.add('hidden');
        const elapsed = byId('execElapsed');
        if (elapsed) elapsed.className = 'lc-elapsed';
        const dot = elapsed && elapsed.querySelector('.lc-dot');
        if (dot) dot.classList.remove('blink');
        const header = byId('execHeader');
        const badge = byId('execBadge');
        const glyph = byId('execFailGlyph');
        const label = byId('execPhaseLabel');
        const icon = byId('termIcon');
        if (icon) icon.classList.toggle('failed', !!failed);
        if (failed) {
            if (header) header.className = 'lc-header failed';
            if (badge) badge.classList.add('hidden');
            if (glyph) glyph.classList.remove('hidden');
            if (label) label.textContent = 'Execution failed';
            setTermState('failed', returncode === null || returncode === undefined
                ? 'failed' : 'failed · exit ' + returncode, false);
            CC.pipeline.set('execute', 'failed', note || 'failed', opts);
            CC.announce('Run failed: ' + (note || 'failed') + '.');
        } else {
            if (header) header.className = 'lc-header done';
            if (badge) badge.classList.remove('hidden');
            if (glyph) glyph.classList.add('hidden');
            if (label) label.textContent = 'Completed in sandbox';
            setTermState('done', 'exit ' + (returncode || 0), false);
            CC.pipeline.set('execute', 'done', 'exit ' + (returncode || 0));
            CC.announce('Run finished, exit ' + (returncode || 0) + '. Interpreting the results.');
        }
    }

    /** The AI debug frame: name the error and offer the two ways forward. The
     *  actual diagnosis streams into Interpretation underneath. */
    function showDebugFrame(output) {
        const frame = byId('debugFrame');
        const summary = byId('debugSummary');
        if (!frame || !summary) return;
        const line = errorSummary(output);
        const culprit = culpritFrom(output);
        let html = CC.escapeHtml(line);
        if (culprit) {
            const tok = CC.escapeHtml(culprit);
            html = html.split("'" + tok + "'").join('<code>' + tok + '</code>');
        }
        const loc = errorLocation(output);
        if (loc) html += ' <span class="lc-trace-dim">(line ' + loc.line + ')</span>';
        summary.innerHTML = html;
        frame.classList.remove('hidden');
    }

    // --- recovery actions (design: "a fix path, never a dead end") ------------
    // Exposed as globals so boot.js's delegated data-action listener can reach
    // them from the pipeline buttons ui.js renders at runtime.

    /** Re-run the current script unchanged. */
    CC.retryRun = function () {
        CC.switchTab('results');
        runScript();
    };
    window.retryRun = CC.retryRun;

    /** Hand the traceback back to STATlee and regenerate. One user-initiated
     *  /chat call in refine mode, so the model patches the failing script
     *  instead of starting over. */
    CC.fixWithAI = function () {
        const run = CC.state.lastRun;
        const output = (run && run.output) || '';
        // Only a real traceback from THIS script is worth a billable
        // regeneration. A refusal/timeout never ran; an older run's error
        // belongs to a script that is no longer in the editor.
        if (!run || run.success || !output.trim()) {
            CC.toast('Run the script first so there is an error to fix.', 'error');
            return;
        }
        if (run.notExecuted) {
            CC.toast('This script never reached the sandbox, so there is no traceback to fix. ' +
                'Retry, or edit the script.', 'error');
            return;
        }
        if (run.code !== undefined && run.code !== CC.getCode()) {
            CC.toast('The script changed since that run. Run the current script first.', 'error');
            return;
        }
        const promptInput = byId('promptInput');
        const refine = byId('refineToggle');
        const btn = byId('generateBtn');
        if (!promptInput || !btn || btn.disabled) return;
        if (refine) refine.checked = true;
        // Tail only: the traceback's last lines carry the cause, and the whole
        // 256KB cap would be unmetered prompt spend.
        const trace = output.length > 2000 ? output.slice(-2000) : output;
        const userPrompt = promptInput.value;
        // The traceback is for the model, not the user's question: name the
        // failed run so the server records the fix under that run's question.
        pendingFixOf = run.run_id || null;
        promptInput.value =
            'The script failed in the sandbox with this error:\n\n' + trace +
            '\n\nFix the cause and return the corrected script.';
        CC.toast('Sent the error back to STATlee. Regenerating the script...', 'info');
        btn.click();
        // generate() read the prompt synchronously; give the user's own
        // question back instead of leaving the traceback in their composer.
        promptInput.value = userPrompt;
    };
    window.fixWithAI = CC.fixWithAI;

    function initEditor() {
        const host = document.getElementById('codeEditorHost');
        CC.editor = CodeMirror(host, {
            // Empty, not a sentinel comment: the Code tab's empty state is a
            // real overlay (#codeEmptyState) and a placeholder line underneath it
            // would show through and read as generated code.
            value: '',
            mode: 'python',
            theme: 'material-darker',
            lineNumbers: true,
            lineWrapping: false,
            indentUnit: 4,
            readOnly: false,
            // contenteditable, not the hidden-textarea default: the editable
            // code is then the focusable thing inside the scroll area, which
            // CodeMirror documents as the better mode for screen readers.
            inputStyle: 'contenteditable',
            screenReaderLabel: 'Generated analysis script (editable)',
        });
        // One hook for every way the editor gains content: the generation stream
        // (CC.setCode -> setValue fires 'change'), and a user typing or pasting
        // their own script through the pass-through veil.
        CC.editor.on('change', () => {
            document.body.classList.toggle('cc-no-code', CC.getCode().trim() === '');
        });
        languageSelect().addEventListener('change', () => {
            const lang = languageSelect().value;
            CC.editor.setOption('mode', lang === 'R' ? 'r' : 'python');
            document.getElementById('scriptFilename').textContent =
                lang === 'R' ? 'script.R' : 'script.py';
        });
    }

    // --- reset -----------------------------------------------------------
    /** Forget every analysis artefact built on the previous dataset: script,
     *  run, interpretation, plots, conversation and report draft. Called by the
     *  upload flow once a NEW dataset has been accepted, so a generated script,
     *  a report or a chat about dataset A can never be paired with dataset B. */
    CC.resetAnalysisState = function () {
        CC.setCode('');
        CC.state.lastRun = null;
        CC.state.latestInterpretation = '';
        CC.state.lastPrompt = '';
        CC.state.generated = null;
        CC.state.report = '';
        CC.state.chatHistory = [];
        CC.state.converseHistory = [];
        ['runSection', 'refineWrap', 'genHeader', 'execHeader', 'debugFrame'].forEach((id) => {
            const el = byId(id);
            if (el) el.classList.add('hidden');
        });
        const copyBtn = byId('copyBtn');
        if (copyBtn) copyBtn.style.display = 'none';
        setTermState(null);
        setInterpretationTitle(false);
        if (CC.clearMethods) CC.clearMethods();
        ['runOutput', 'interpretationOutput', 'plotOutput'].forEach((id) => {
            const el = byId(id);
            if (el) el.innerHTML = '';
        });
        const editor = byId('reportEditor');
        if (editor) editor.value = '';
        const preview = byId('reportPreview');
        if (preview) preview.innerHTML = '';
        const log = byId('chatLog');
        if (log) {
            Array.from(log.children).forEach((child) => {
                if (child.id !== 'converseEmptyState') child.remove();
            });
        }
        document.body.classList.add('cc-no-code', 'cc-no-run', 'cc-no-chat');
        // The workbook keeps its runs (they live on the server), but nothing
        // from the previous dataset stays open in Results.
        if (CC.workbook) CC.workbook.reset();
    };

    // --- generate ----------------------------------------------------------
    let generating = false;
    // Set by fixWithAI for the one generation it starts: the run being fixed.
    let pendingFixOf = null;
    CC.isGenerating = function () { return generating; };

    async function generate() {
        // Read and cleared first, so a refused click never carries it over.
        const fixOf = pendingFixOf;
        pendingFixOf = null;
        if (generating) return;            // One generation at a time
        const promptText = document.getElementById('promptInput').value;
        const language = languageSelect().value;
        if (!CC.state.filename || !promptText.trim()) {
            CC.toast('Upload data and enter a prompt first.', 'error');
            return;
        }
        const generateBtn = document.getElementById('generateBtn');
        const loading = document.getElementById('loading');
        const runSection = document.getElementById('runSection');
        // The editor is the user's work. Everything below that fails
        // (refusal, stream error, a cut connection) puts this back verbatim.
        const before = CC.getCode();
        const runWasVisible = !runSection.classList.contains('hidden');
        generating = true;
        generateBtn.disabled = true;
        loading.classList.remove('hidden');
        CC.switchTab('generate');
        runSection.classList.add('hidden');
        CC.pipeline.set('generate', 'active', 'drafting');

        function restore(message) {
            setGenPhase(null);
            CC.setCode(before);
            // The old script is still runnable if it was before this attempt.
            if (runWasVisible && before.trim()) runSection.classList.remove('hidden');
            CC.pipeline.set('generate', 'failed', message);
            CC.toast(message, 'error');
            CC.announce('Generation failed. Your previous script was kept.');
        }

        // Merge codebook + descriptions (RAG context for the backend)
        const mergedCodebook = {};
        Object.entries(CC.state.codebook).forEach(([col, classification]) => {
            const colLower = col.toLowerCase();
            const matchedKey = Object.keys(CC.state.pdfMapping)
                .find(k => k.toLowerCase() === colLower);
            mergedCodebook[col] = {
                type: classification,
                description: matchedKey ? CC.state.pdfMapping[matchedKey] : null,
            };
        });

        // Refinement, i.e. include the current editor contents so the
        // model modifies instead of regenerating.
        const refineOn = document.getElementById('refineToggle').checked;
        const existing = CC.getCode();
        // The editor starts empty (no sentinel comment), so `existing` being
        // truthy is the "there is a script to refine" test.
        const sendCurrent = refineOn && CC.state.chatHistory.length > 0 && !!existing;

        try {
            const response = await CC.postStream('/chat', {
                filename: CC.state.filename,
                language,
                prompt: promptText,
                history: CC.state.chatHistory,
                codebook: mergedCodebook,
                current_code: sendCurrent ? existing : null,
                pro: CC.proOn(),
                fix_of: fixOf,
            });

            const contentType = response.headers.get('Content-Type') || '';
            if (!contentType.includes('text/event-stream')) {
                const data = await CC.readJSON(response);
                restore(CC.errorMessage(response.status, data, 'Generation failed'));
                return;
            }

            let accumulated = '';
            let finalCode = null;
            let finalQuestion = null;
            let streamError = null;

            await CC.streamSSE(response, {
                onPhase: (phase, message) => {
                    loading.classList.add('hidden');
                    // Not every phase is a generation step. The feature-selection
                    // fallback notice carries a message and must not reset the editor
                    // or mislabel the header as a drafting phase.
                    if (phase === 'feature_selection_skipped') {
                        CC.toast(message || 'Column pre-selection was unavailable; '
                            + 'analyzing with the full dataset schema.', 'info');
                        return;
                    }
                    accumulated = '';
                    CC.setCode('');
                    setGenPhase(phase === 'drafting' ? 'Drafting script' : 'Validating script');
                    CC.pipeline.set('generate', 'active',
                        phase === 'drafting' ? 'drafting' : 'validating');
                },
                onDelta: (text) => {
                    loading.classList.add('hidden');
                    setStreaming(true);
                    accumulated += text;
                    CC.setCode(accumulated);
                    CC.editor.scrollTo(null, CC.editor.getScrollInfo().height);
                },
                onDone: (data) => {
                    finalCode = data.code;
                    if (typeof data.question === 'string') finalQuestion = data.question;
                    if (data.usage) CC.addUsage(data.usage);
                },
                onError: (msg) => { streamError = msg; },
            });

            if (streamError || finalCode === null) {
                // A stream that ended without `done` is a failure too;
                // its partial deltas are not a script.
                restore(streamError || 'Generation did not finish.');
                return;
            }
            setGenPhase(null);
            CC.setCode(finalCode);
            document.getElementById('copyBtn').style.display = 'flex';
            runSection.classList.remove('hidden');
            document.getElementById('refineWrap').classList.remove('hidden');
            const frame = byId('debugFrame');
            if (frame) frame.classList.add('hidden');   // a fresh script, an old diagnosis
            CC.state.chatHistory.push({ role: 'user', text: promptText });
            CC.state.chatHistory.push({ role: 'model', text: finalCode });
            // A fix keeps the question it repaired (the server's record), so
            // /history never files the run under the traceback.
            CC.state.lastPrompt = fixOf ? (finalQuestion || CC.state.lastPrompt) : promptText;
            // Remember what language the server generated (and approved)
            // this script in, so Run uses it even if the selector moves.
            CC.state.generated = { code: finalCode, language };
            // A new script makes any previous run's verdict stale.
            CC.pipeline.set('execute', 'pending', 'waiting for script');
            const filename = document.getElementById('scriptFilename');
            CC.pipeline.set('generate', 'done',
                (filename ? filename.textContent + ' · ' : '') +
                finalCode.split('\n').length + ' lines');
            CC.announce('Script ready, ' + finalCode.split('\n').length + ' lines.');
        } catch (err) {
            restore('Network error: generation did not reach the server.');
        } finally {
            generating = false;
            generateBtn.disabled = false;
            loading.classList.add('hidden');
            setStreaming(false);
        }
    }

    // --- run + interpret -------------------------------------------------------
    const B64 = /^[A-Za-z0-9+/]+={0,2}$/;
    function renderPlots(input) {
        // Only well-formed base64 reaches the data: URL below.
        const plots = (input || []).filter((p) => typeof p === 'string' && B64.test(p));
        if (plots.length === 0) return '';
        const label = plots.length > 1
            ? `Generated Visualizations (${plots.length})` : 'Generated Visualization';
        let html = `<div class="mt-8 border-t border-slate-200 dark:border-slate-800 pt-6">
            <h2 class="text-[10px] font-mono uppercase text-[var(--text-3)] font-bold mb-4 tracking-widest">${label}</h2>
            <div class="flex flex-col gap-6">`;
        plots.forEach((p, i) => {
            html += `<img src="data:image/png;base64,${p}" alt="Generated plot ${i + 1}" class="max-w-full rounded-xl border border-slate-200 shadow-xl dark:border-slate-700 bg-white">`;
        });
        return html + '</div></div>';
    }

    const RUN_LABEL = '<svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clip-rule="evenodd"></path></svg> Run in sandbox';
    let running = false;
    let interpreting = false;
    /** True while a run or an interpretation is writing into Results: the
     *  workbook must not swap another run into those panels meanwhile. */
    CC.resultsBusy = function () { return running || interpreting; };

    /** Language to run with. If the editor still holds the script the
     *  server generated, run it in the language it was generated (and
     *  approved) in, even if the selector was moved since. */
    function runLanguage(code) {
        const gen = CC.state.generated;
        if (gen && gen.code === code && gen.language) return gen.language;
        return languageSelect().value;
    }
    // Approval is keyed on (language, code): /export must name the
    // same language, or it will not recognise the reviewed editor script.
    CC.runLanguage = runLanguage;

    /** A timed-out run DID run: its outcome was recorded, so it is a
     *  failed run, not a refusal. /run answers 200 with `timed_out: true`
     *  and the output printed before the stop. */
    function timeoutReply(ok, data) {
        return ok && !!(data && data.timed_out);
    }

    /** Wording for a run that never reached the sandbox. Any
     *  script /chat did not approve in this session (a restored history
     *  entry, a paste, an edit) gets a safety re-check first: 403 when the
     *  check rejects it, 503 when the check itself is unavailable. */
    function notRunMessage(status, data) {
        const server = CC.errorMessage(status, data, 'The script was not run');
        if (status === 503) {
            return 'The safety re-check that runs before a new or edited script could not '
                + 'be completed right now, so the script was not run. Your script is '
                + 'unchanged; press Retry in a moment. (' + server + ')';
        }
        if (status === 403) {
            return server + ' The script was not run. Edit it, or generate a new one.';
        }
        return server;
    }

    async function runScript() {
        if (running) return;               // One run at a time
        if (interpreting) {                // a saved run's interpretation is streaming
            CC.toast('Wait for the interpretation to finish, then run the script.', 'info');
            return;
        }
        const code = CC.getCode();
        if (!code.trim()) {
            CC.toast('There is no script to run yet.', 'error');
            return;
        }
        const language = runLanguage(code);
        const runBtn = document.getElementById('runBtn');
        const runOutput = document.getElementById('runOutput');
        const interpretationOutput = document.getElementById('interpretationOutput');
        const plotOutput = byId('plotOutput');

        running = true;
        // A new run takes Results back from any saved run being viewed.
        if (CC.workbook) CC.workbook.leaveView();
        runBtn.disabled = true;
        runBtn.innerHTML = `${CC.spinner('h-4 w-4')} Running...`;
        CC.switchTab('results');
        runOutput.innerText = 'Running the script in the sandbox...';
        if (plotOutput) plotOutput.innerHTML = '';
        // The previous run's Methods card must not sit beside this one.
        if (CC.clearMethods) CC.clearMethods();
        execRunning();
        interpretationOutput.innerHTML =
            `<span class="font-mono text-xs uppercase tracking-widest text-indigo-500 flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} Awaiting output stream...</span>`;

        try {
            const { ok, status, data } = await CC.post('/run', {
                code, language, filename: CC.state.filename,
            });
            if (timeoutReply(ok, data)) {
                // It ran and was stopped. The server recorded the outcome
                // (returncode / timed_out), so /interpret diagnoses it.
                const out = String(data.output || '');
                const plots = Array.isArray(data.plots) ? data.plots : [];
                const runId = data.run_id || null;
                CC.state.lastRun = { output: out, plots, success: false, timedOut: true, code, run_id: runId };
                if (CC.workbook && runId) CC.workbook.noteLiveRun(runId, data.evicted);
                renderTerminal(out, true);
                if (CC.renderMethods) CC.renderMethods(data.methods, { failed: true });
                execFinished(true, null, 'timed out', { noFix: true });
                if (plotOutput) plotOutput.innerHTML = renderPlots(plots);
                interpretationOutput.innerHTML =
                    `<span class="font-mono text-xs uppercase tracking-widest text-indigo-500 flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} Diagnosing the failure...</span>`;
                await interpret({ output: out, plots, success: false }, code, language, { runId });
                return;
            }
            if (!ok) {
                // Never reached the sandbox (run-guard refusal, re-check
                // outage, 5xx): there is no exit code and no traceback, so
                // "Fix with AI" would pay for a regeneration against nothing.
                const msg = notRunMessage(status, data);
                runOutput.innerText = msg;
                CC.state.lastRun = { output: msg, plots: [], success: false, notExecuted: true, code };
                execFinished(true, null, 'not executed', { noFix: true });
                interpretationOutput.textContent = 'Nothing to interpret: the script was not run.';
                CC.toast(msg, 'error');
                return;
            }
            const runId = data.run_id || null;
            CC.state.lastRun = {
                output: data.output || '', plots: data.plots || [], success: !!data.success, code,
                run_id: runId,
            };
            if (CC.workbook && runId) CC.workbook.noteLiveRun(runId, data.evicted);
            const failed = !data.success;
            renderTerminal(data.output, failed);
            // The server's parsed methods block, above the terminal.
            if (CC.renderMethods) CC.renderMethods(data.methods, { failed });
            execFinished(failed, data.returncode,
                failed ? failureNote(data.output) : null);
            if (failed) showDebugFrame(data.output || '');
            // Plots render once into their own container, not again on
            // every streamed interpretation chunk.
            if (plotOutput) plotOutput.innerHTML = renderPlots(data.plots);
            interpretationOutput.innerHTML =
                `<span class="font-mono text-xs uppercase tracking-widest text-indigo-500 flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} ${data.success ? 'Analyzing terminal results...' : 'Diagnosing the failure...'}</span>`;

            await interpret(data, code, language, { runId });
        } catch (err) {
            const msg = 'Network error: the run request did not reach the server.';
            runOutput.innerText = msg;
            CC.state.lastRun = { output: msg, plots: [], success: false, notExecuted: true, code };
            execFinished(true, null, 'network error', { noFix: true });
        } finally {
            running = false;
            runBtn.disabled = false;
            runBtn.innerHTML = RUN_LABEL;
        }
    }

    /** "Interpretation" for a successful run, "Diagnosis" for a failed one.
     *  The server decides from the run's real exit code / timeout and
     *  says so in `done.debug`; until then the client's own verdict is used. */
    function setInterpretationTitle(diagnosis) {
        const el = byId('interpretationTitle');
        if (el) el.textContent = diagnosis ? 'Diagnosis' : 'Interpretation';
    }
    CC.setInterpretationTitle = setInterpretationTitle;

    /** Stream an interpretation into Results.
     *
     *  `opts.runId` names the workbook run it grounds on: the server
     *  reads that run's own output and stores the interpretation on it. With
     *  `opts.saved` the run is an older one open from the rail: the live
     *  session state (latestInterpretation, lastRun, history) is left alone.
     *  Resolves to the interpretation text, or '' when there is none. */
    async function interpret(runData, code, language, opts) {
        const o = opts || {};
        const interpretationOutput = document.getElementById('interpretationOutput');
        interpretationOutput.setAttribute('aria-busy', 'true');
        interpreting = true;
        let debug = !runData.success;
        setInterpretationTitle(debug);
        // With a run_id the server grounds on that run's own stored output,
        // plots, script and exit code and ignores any client copy, so none
        // of it is uploaded again (a run's plots can be ~13 MB of base64,
        // enough to hit MAX_UPLOAD_MB after a successful run).
        const body = o.runId
            ? { run_id: o.runId }
            : {
                output: runData.output || '',
                plots: runData.plots || [],
                success: !!runData.success,
                code,  // Lets the backend switch to debugging-assistant mode
            };
        try {
            const response = await CC.postStream('/interpret', body);
            const contentType = response.headers.get('Content-Type') || '';
            if (!contentType.includes('text/event-stream')) {
                const data = await CC.readJSON(response);
                if (!response.ok || !data.interpretation) {
                    // An error body is not an interpretation.
                    interpretationOutput.textContent = 'Interpretation failed: ' +
                        CC.errorMessage(response.status, data, 'no interpretation returned');
                    return '';
                }
                if (!o.saved) CC.state.latestInterpretation = data.interpretation;
                interpretationOutput.innerHTML = CC.renderMarkdown(data.interpretation);
                CC.announce('Interpretation ready.');
                return data.interpretation;
            }
            const scrollContainer = document.getElementById('contentResults');
            let accumulated = '';
            let streamError = null;
            await CC.streamSSE(response, {
                onDelta: (text) => {
                    accumulated += text;
                    interpretationOutput.innerHTML = CC.renderMarkdown(accumulated);
                    if (scrollContainer) scrollContainer.scrollTop = scrollContainer.scrollHeight;
                },
                onDone: (data) => {
                    if (!o.saved) CC.state.latestInterpretation = accumulated;
                    if (data.usage) CC.addUsage(data.usage);
                    if (typeof data.debug === 'boolean') {
                        debug = data.debug;
                        setInterpretationTitle(debug);
                        // The server's verdict comes from the recorded exit code,
                        // so an R error or a timeout does not read as success.
                        if (!o.saved && CC.state.lastRun && debug) CC.state.lastRun.success = false;
                    }
                },
                onError: (msg) => { streamError = msg; },
            });
            if (streamError) {
                interpretationOutput.textContent = 'Error generating interpretation: ' + streamError;
                CC.announce('Interpretation failed.');
                return '';
            }
            CC.announce('Interpretation ready.');
            if (!o.saved) {
                CC.recordHistory({
                    prompt: CC.state.lastPrompt,
                    code,
                    language,
                    dataset_name: CC.state.filename,      // stored name: what a re-run needs
                    dataset_label: CC.datasetLabel(),     // the user's name for it
                    output: runData.output || '',
                    interpretation: CC.state.latestInterpretation,
                    success: !debug,      // restore it as a failed run, not a success
                    run_id: o.runId || undefined,         // Restore brings back plots
                });
            }
            return accumulated;
        } catch (err) {
            interpretationOutput.textContent = 'Network error: the interpretation request failed.';
            return '';
        } finally {
            interpreting = false;
            interpretationOutput.removeAttribute('aria-busy');
        }
    }

    // --- saved workbook runs -------------------------------------------
    /** A saved run failed when it timed out or exited non-zero. A run with no
     *  recorded exit code (an adopted legacy run) is not claimed either way. */
    function savedRunFailed(run) {
        return !!run.timed_out || (typeof run.returncode === 'number' && run.returncode !== 0);
    }
    CC.savedRunFailed = savedRunFailed;

    /** Render a saved run (GET /runs/<id>) into Results: terminal, plots and
     *  its own stored interpretation. Display only: the editor, the pipeline
     *  and the live run state are not touched. */
    CC.renderSavedRun = function (run) {
        const failed = savedRunFailed(run);
        const plots = Array.isArray(run.plots) ? run.plots : [];
        document.body.classList.remove('cc-no-run');
        ['execHeader', 'debugFrame', 'execProgress'].forEach((id) => {
            const el = byId(id);
            if (el) el.classList.add('hidden');
        });
        const icon = byId('termIcon');
        if (icon) icon.classList.toggle('failed', failed);
        renderTerminal(run.output || '', failed);
        if (CC.renderMethods) CC.renderMethods(run.methods, { failed });
        if (run.timed_out) setTermState('failed', 'saved · timed out', false);
        else if (typeof run.returncode !== 'number') setTermState('done', 'saved run', false);
        else if (failed) setTermState('failed', 'saved · exit ' + run.returncode, false);
        else setTermState('done', 'saved · exit ' + run.returncode, false);
        const plotOutput = byId('plotOutput');
        if (plotOutput) plotOutput.innerHTML = renderPlots(plots);
        const out = byId('interpretationOutput');
        setInterpretationTitle(failed);
        if (!out) return;
        if (run.interpretation && run.interpretation.trim()) {
            out.innerHTML = CC.renderMarkdown(run.interpretation);
        } else {
            out.textContent = (run.output || '').trim() || plots.length
                ? 'No interpretation was saved for this run.'
                : 'This run produced no output to interpret.';
        }
    };

    /** Interpret a saved run that has none yet, grounded on that run. */
    CC.interpretSavedRun = function (run) {
        const out = byId('interpretationOutput');
        if (out) {
            out.innerHTML = `<span class="font-mono text-xs uppercase tracking-widest text-indigo-500 flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} Analyzing the saved run...</span>`;
        }
        // The server grounds on the run's stored output and plots; the
        // client copy only has to say what is being interpreted.
        return interpret({ output: run.output || '', plots: [], success: !savedRunFailed(run) },
            run.code || '', run.language || 'Python', { runId: run.run_id, saved: true });
    };

    /** "Load script": put a saved run's script in the editor, in its own
     *  language, ready to run again. */
    CC.loadRunScript = function (run) {
        const lang = run.language === 'R' ? 'R' : 'Python';
        const select = languageSelect();
        if (select && select.value !== lang) {
            select.value = lang;
            select.dispatchEvent(new Event('change'));
        }
        CC.setCode(run.code || '');
        // Same record a generation leaves, so Run uses this script's language.
        CC.state.generated = { code: run.code || '', language: lang };
        if (run.prompt) CC.state.lastPrompt = run.prompt;
        const copyBtn = byId('copyBtn');
        if (copyBtn) copyBtn.style.display = 'flex';
        const runSection = byId('runSection');
        if (runSection) runSection.classList.remove('hidden');
        CC.switchTab('generate');
    };

    document.addEventListener('DOMContentLoaded', () => {
        initEditor();
        document.getElementById('generateBtn').addEventListener('click', generate);
        document.getElementById('runBtn').addEventListener('click', runScript);

        // AI debug frame: the two ways out of a failed run.
        const fixBtn = byId('debugFixBtn');
        if (fixBtn) fixBtn.addEventListener('click', CC.fixWithAI);
        const editBtn = byId('debugEditBtn');
        if (editBtn) {
            editBtn.addEventListener('click', () => {
                CC.switchTab('generate');
                if (CC.editor) CC.editor.focus();
            });
        }
        // Keep the model chip honest if Pro mode is toggled mid-session.
        const proToggle = byId('proToggle');
        if (proToggle) proToggle.addEventListener('change', showModelChip);

        // The label is read once, so a second click within the 2 s window
        // cannot capture "Copied!" as the label to restore.
        const copyBtn = document.getElementById('copyBtn');
        const copyLabel = copyBtn.innerHTML;
        let copyTimer = null;
        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(CC.getCode());
            } catch (e) {
                // Clipboard permission denied / insecure context: say so
                // instead of claiming "Copied!".
                CC.toast('Could not copy to the clipboard. Select the code and copy it manually.', 'error');
                return;
            }
            copyBtn.innerHTML = '<svg class="w-4 h-4 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg> Copied!';
            clearTimeout(copyTimer);
            copyTimer = setTimeout(() => { copyBtn.innerHTML = copyLabel; }, 2000);
        });
    });
}());
