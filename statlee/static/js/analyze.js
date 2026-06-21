/* Analysis pipeline UI: editable CodeMirror editor (5.4), phase-labelled
 * streaming generation (5.5), refinement mode (5.12), guarded execution,
 * multi-plot gallery (5.2), streamed interpretation incl. auto-debug (5.11),
 * and history capture (5.7). */
(function () {
    'use strict';

    // "Pro mode" toggle: when on, the backend runs code generation on the
    // bigger gemini-3.1-pro model instead of the default. Read once per request.
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

    function initEditor() {
        const host = document.getElementById('codeEditorHost');
        CC.editor = CodeMirror(host, {
            value: '# System ready. Upload a dataset to begin...',
            mode: 'python',
            theme: 'material-darker',
            lineNumbers: true,
            lineWrapping: false,
            indentUnit: 4,
            readOnly: false,
            screenReaderLabel: 'Generated analysis script (editable)',
        });
        languageSelect().addEventListener('change', () => {
            const lang = languageSelect().value;
            CC.editor.setOption('mode', lang === 'R' ? 'r' : 'python');
            document.getElementById('scriptFilename').textContent =
                lang === 'R' ? 'script.R' : 'script.py';
        });
    }

    function setGenPhase(label) {
        const el = document.getElementById('genPhase');
        if (!el) return;
        if (label) {
            el.classList.remove('hidden');
            el.innerHTML = `${CC.spinner('h-3.5 w-3.5')} <span class="animate-pulse uppercase tracking-wider">${label}</span>`;
        } else {
            el.classList.add('hidden');
        }
    }

    // --- generate ----------------------------------------------------------
    async function generate() {
        const promptText = document.getElementById('promptInput').value;
        const language = languageSelect().value;
        if (!CC.state.filename || !promptText) {
            CC.toast('Upload data and enter a prompt first.', 'error');
            return;
        }
        const generateBtn = document.getElementById('generateBtn');
        const loading = document.getElementById('loading');
        generateBtn.disabled = true;
        loading.classList.remove('hidden');
        CC.switchTab('generate');
        document.getElementById('runSection').classList.add('hidden');

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

        // 5.12: refinement — include the current editor contents so the model
        // modifies instead of regenerating.
        const refineOn = document.getElementById('refineToggle').checked;
        const existing = CC.getCode();
        const sendCurrent = refineOn && CC.state.chatHistory.length > 0 &&
            existing && !existing.startsWith('# System ready');

        let streamedAny = false;
        try {
            const response = await CC.postStream('/chat', {
                filename: CC.state.filename,
                language,
                prompt: promptText,
                history: CC.state.chatHistory,
                codebook: mergedCodebook,
                current_code: sendCurrent ? existing : null,
                pro: CC.proOn(),
            });

            const contentType = response.headers.get('Content-Type') || '';
            if (!contentType.includes('text/event-stream')) {
                const data = await response.json();
                CC.setCode('# Error: ' + (data.error || 'Unknown error.'));
                CC.toast(data.error || 'Generation failed.', 'error');
                return;
            }

            let accumulated = '';
            let finalCode = null;
            let streamError = null;

            await CC.streamSSE(response, {
                onPhase: (phase) => {
                    loading.classList.add('hidden');
                    accumulated = '';
                    CC.setCode('');
                    setGenPhase(phase === 'drafting' ? 'Drafting script…' : 'Validating script…');
                },
                onDelta: (text) => {
                    streamedAny = true;
                    loading.classList.add('hidden');
                    accumulated += text;
                    CC.setCode(accumulated);
                    CC.editor.scrollTo(null, CC.editor.getScrollInfo().height);
                },
                onDone: (data) => {
                    finalCode = data.code;
                    if (data.usage) CC.addUsage(data.usage);
                },
                onError: (msg) => { streamError = msg; },
            });

            setGenPhase(null);
            if (streamError) {
                CC.setCode('# Error: ' + streamError);
                CC.toast(streamError, 'error');
            } else if (finalCode !== null) {
                CC.setCode(finalCode);
                document.getElementById('copyBtn').style.display = 'flex';
                document.getElementById('runSection').classList.remove('hidden');
                document.getElementById('refineWrap').classList.remove('hidden');
                CC.state.chatHistory.push({ role: 'user', text: promptText });
                CC.state.chatHistory.push({ role: 'model', text: finalCode });
                CC.state.lastPrompt = promptText;
            }
        } catch (err) {
            setGenPhase(null);
            if (!streamedAny) CC.setCode('# Network error.');
            CC.toast('Network error during generation.', 'error');
        } finally {
            generateBtn.disabled = false;
            loading.classList.add('hidden');
        }
    }

    // --- run + interpret -------------------------------------------------------
    function renderPlots(plots) {
        if (!plots || plots.length === 0) return '';
        const label = plots.length > 1
            ? `Generated Visualizations (${plots.length})` : 'Generated Visualization';
        let html = `<div class="mt-8 border-t border-slate-200 dark:border-slate-800 pt-6">
            <h3 class="text-[10px] font-mono uppercase text-slate-400 font-bold mb-4 tracking-widest">${label}</h3>
            <div class="flex flex-col gap-6">`;
        plots.forEach((p, i) => {
            html += `<img src="data:image/png;base64,${p}" alt="Generated plot ${i + 1}" class="max-w-full rounded-xl border border-slate-200 shadow-xl dark:border-slate-700 bg-white">`;
        });
        return html + '</div></div>';
    }

    async function runScript() {
        const code = CC.getCode();
        const language = languageSelect().value;
        const runBtn = document.getElementById('runBtn');
        const runOutput = document.getElementById('runOutput');
        const interpretationOutput = document.getElementById('interpretationOutput');

        runBtn.disabled = true;
        runBtn.innerHTML = `${CC.spinner('h-4 w-4')} Running...`;
        CC.switchTab('results');
        runOutput.innerText = 'Executing script in local sandbox...';
        interpretationOutput.innerHTML =
            `<span class="font-mono text-xs uppercase tracking-widest text-indigo-500 flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} Awaiting output stream...</span>`;

        try {
            const { ok, data } = await CC.post('/run', {
                code, language, filename: CC.state.filename,
            });
            if (!ok) {
                runOutput.innerText = 'Error:\n' + (data.error || 'Unknown');
                interpretationOutput.innerHTML = 'Cannot interpret due to execution failure.';
                return;
            }
            CC.state.lastRun = {
                output: data.output || '', plots: data.plots || [], success: !!data.success,
            };
            runOutput.innerText = data.output || 'Execution successful.';
            const plotHTML = renderPlots(data.plots);
            interpretationOutput.innerHTML =
                `<span class="font-mono text-xs uppercase tracking-widest text-indigo-500 flex items-center gap-2">${CC.spinner('h-3.5 w-3.5')} ${data.success ? 'Analyzing terminal results...' : 'Diagnosing the failure...'}</span>` + plotHTML;

            await interpret(data, plotHTML, code, language);
        } catch (err) {
            runOutput.innerText = 'Connection failed.';
        } finally {
            runBtn.disabled = false;
            runBtn.innerHTML = '<svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clip-rule="evenodd"></path></svg> Execute Script Locally';
        }
    }

    async function interpret(runData, plotHTML, code, language) {
        const interpretationOutput = document.getElementById('interpretationOutput');
        try {
            const response = await CC.postStream('/interpret', {
                output: runData.output || '',
                plots: runData.plots || [],
                success: !!runData.success,
                code,  // 5.11: lets the backend switch to debugging-assistant mode
            });
            const contentType = response.headers.get('Content-Type') || '';
            if (!contentType.includes('text/event-stream')) {
                const data = await response.json();
                CC.state.latestInterpretation = data.interpretation || 'No interpretation generated.';
                interpretationOutput.innerHTML =
                    CC.renderMarkdown(CC.state.latestInterpretation) + plotHTML;
                return;
            }
            const scrollContainer = document.getElementById('contentResults');
            let accumulated = '';
            let streamError = null;
            await CC.streamSSE(response, {
                onDelta: (text) => {
                    accumulated += text;
                    interpretationOutput.innerHTML = CC.renderMarkdown(accumulated) + plotHTML;
                    if (scrollContainer) scrollContainer.scrollTop = scrollContainer.scrollHeight;
                },
                onDone: (data) => {
                    CC.state.latestInterpretation = accumulated;
                    if (data.usage) CC.addUsage(data.usage);
                },
                onError: (msg) => { streamError = msg; },
            });
            if (streamError) {
                interpretationOutput.innerHTML =
                    'Error generating interpretation: ' + CC.escapeHtml(streamError) + plotHTML;
            } else {
                CC.recordHistory({
                    prompt: CC.state.lastPrompt,
                    code,
                    language,
                    dataset_name: CC.state.filename,
                    output: runData.output || '',
                    interpretation: CC.state.latestInterpretation,
                });
            }
        } catch (err) {
            interpretationOutput.innerHTML = 'Failed to connect to AI for interpretation.' + plotHTML;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        initEditor();
        document.getElementById('generateBtn').addEventListener('click', generate);
        document.getElementById('runBtn').addEventListener('click', runScript);
        document.getElementById('copyBtn').addEventListener('click', () => {
            navigator.clipboard.writeText(CC.getCode());
            const btn = document.getElementById('copyBtn');
            const orig = btn.innerHTML;
            btn.innerHTML = '<svg class="w-4 h-4 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg> Copied!';
            setTimeout(() => { btn.innerHTML = orig; }, 2000);
        });
    });
}());
