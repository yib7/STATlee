/* Converse tab: single-pass streamed chat (4.2) with sanitized markdown
 * bubbles (0.5) and the hypothesis-coach guide mode (5.15). */
(function () {
    'use strict';

    const BUBBLE_MODEL = 'self-start bg-white/95 text-slate-900 border border-slate-300 dark:border-slate-700/50 dark:bg-[#161332]/90 backdrop-blur-md dark:text-slate-200 p-5 rounded-2xl rounded-tl-none max-w-[80%] text-base shadow-lg leading-relaxed font-medium markdown-body';
    const BUBBLE_ERROR = 'self-start bg-red-50 text-red-700 border border-red-200 dark:bg-red-900/30 dark:border-red-900/50 dark:text-red-300 p-5 rounded-2xl rounded-tl-none max-w-[80%] text-base font-medium shadow-lg animate-fade-in-up';

    async function sendMessage() {
        const chatInput = document.getElementById('chatInput');
        const chatLog = document.getElementById('chatLog');
        const text = chatInput.value.trim();
        if (!text) return;

        const guideMode = document.getElementById('guideToggle').checked;

        const userBubble = document.createElement('div');
        userBubble.className = 'self-end bg-indigo-700 text-white p-5 rounded-2xl rounded-tr-none max-w-[80%] text-base shadow-lg animate-fade-in-up font-medium';
        userBubble.textContent = text;
        chatLog.appendChild(userBubble);
        chatInput.value = '';
        chatLog.scrollTop = chatLog.scrollHeight;

        const typing = document.createElement('div');
        typing.className = BUBBLE_MODEL + ' flex gap-2';
        typing.setAttribute('aria-label', 'Assistant is typing');
        typing.innerHTML = '<div class="w-2 h-2 bg-indigo-500 rounded-full animate-bounce"></div><div class="w-2 h-2 bg-indigo-500 rounded-full animate-bounce" style="animation-delay: 0.2s"></div><div class="w-2 h-2 bg-indigo-500 rounded-full animate-bounce" style="animation-delay: 0.4s"></div>';
        chatLog.appendChild(typing);
        chatLog.scrollTop = chatLog.scrollHeight;

        const removeTyping = () => typing.remove();

        function errorBubble(msg) {
            const el = document.createElement('div');
            el.className = BUBBLE_ERROR;
            el.textContent = msg;
            chatLog.appendChild(el);
            chatLog.scrollTop = chatLog.scrollHeight;
        }

        try {
            const res = await CC.postStream('/converse', {
                message: text,
                history: CC.state.converseHistory,
                context: CC.state.latestInterpretation || 'No results generated yet.',
                code: CC.getCode(),
                mode: guideMode ? 'guide' : 'chat',
            });

            const contentType = res.headers.get('Content-Type') || '';
            if (!contentType.includes('text/event-stream')) {
                const data = await res.json();
                removeTyping();
                errorBubble(data.error || 'Error generating response.');
                return;
            }

            let accumulated = '';
            let bubbleEl = null;
            let streamError = null;

            await CC.streamSSE(res, {
                onDelta: (delta) => {
                    if (!bubbleEl) {
                        removeTyping();
                        bubbleEl = document.createElement('div');
                        bubbleEl.className = BUBBLE_MODEL + ' animate-fade-in-up';
                        chatLog.appendChild(bubbleEl);
                    }
                    accumulated += delta;
                    bubbleEl.innerHTML = CC.renderMarkdown(accumulated);
                    chatLog.scrollTop = chatLog.scrollHeight;
                },
                onDone: (data) => {
                    if (accumulated) {
                        CC.state.converseHistory.push({ role: 'user', text });
                        CC.state.converseHistory.push({ role: 'model', text: accumulated });
                    }
                    if (data.usage) CC.addUsage(data.usage);
                },
                onError: (msg) => { streamError = msg; },
            });

            if (streamError) {
                removeTyping();
                errorBubble(streamError);
            }
        } catch (e) {
            removeTyping();
            errorBubble('Connection error.');
        }
        chatLog.scrollTop = chatLog.scrollHeight;
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('chatSend').addEventListener('click', sendMessage);
        document.getElementById('chatInput').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') sendMessage();
        });
        document.getElementById('guideToggle').addEventListener('change', (e) => {
            const chatLog = document.getElementById('chatLog');
            if (e.target.checked) {
                const note = document.createElement('div');
                note.className = BUBBLE_MODEL;
                note.innerHTML = CC.renderMarkdown(
                    "**Guide mode on.** Tell me your hunch — e.g. *\"I think income affects voting\"* — and I'll help you turn it into a rigorous, ready-to-run analysis prompt using your actual columns.");
                chatLog.appendChild(note);
                chatLog.scrollTop = chatLog.scrollHeight;
            }
        });
    });
}());
