"""Flask blueprints (roadmap 3.1) and small shared route helpers."""
import json
import re

from flask import Response, jsonify, stream_with_context


def json_error(message, status=400):
    """Generic client-safe error body (1.6 — never leak str(e))."""
    return jsonify({'error': message}), status


_MODERATION_FALLBACK = ('The request could not be verified as safe and '
                        'on-topic. Please rephrase and try again.')


def moderation_blocked(result_text):
    """Interpret a moderation verdict, **failing closed**.

    Moderation prompts now return a structured JSON verdict
    ``{"decision": "pass" | "block", "reason": "..."}``. This parses it and
    treats anything that is not an explicit ``pass`` — malformed JSON, a missing
    or unexpected ``decision``, an empty body — as *blocked*. That closes the
    prompt-injection path where suppressing a magic word made moderation
    fail open.

    Case handling (P1-6): the prompt schema (``prompts._VERDICT_SHAPE``)
    specifies lowercase ``"pass"``/``"block"``, but ``decision`` is compared
    case-INSENSITIVELY on purpose. This is deliberate slack, not a bug: it is
    the safe direction to differ — a model that returns ``"Pass"`` or ``"PASS"``
    is honored as a pass rather than fail-closed into blocking a legitimate
    request, while any non-pass token (however cased) still blocks. Do NOT
    tighten this to an exact-case match; that would let a stray capital letter
    reject valid analyses.

    Returns ``(blocked: bool, reason: str)``; ``reason`` is empty when allowed.
    """
    try:
        verdict = json.loads((result_text or '').strip())
    except (ValueError, TypeError):
        return True, _MODERATION_FALLBACK

    decision = None
    reason = ''
    if isinstance(verdict, dict):
        decision = str(verdict.get('decision', '')).strip().lower()
        reason = str(verdict.get('reason', '') or '').strip()
    elif isinstance(verdict, str):
        decision = verdict.strip().lower()

    if decision == 'pass':
        return False, ''
    return True, (reason or _MODERATION_FALLBACK)


# ---------------------------------------------------------------------------
# P1-7: server-side caps on client-supplied prompt material
# ---------------------------------------------------------------------------
# Every free-text field a client POSTs flows into an LLM prompt billed to the
# operator's key; before these caps the only bound was the global 16 MB
# request limit, so one request inside the normal rate limit could smuggle
# megabytes of attacker-chosen text (millions of input tokens) into a paid
# call. Sizing follows the /history save caps in routes/auth.py (10k/20k).
FREE_TEXT_MAX = 20000       # analysis output, code, context, chat messages
BACKGROUND_MAX = 5000       # short framing text (e.g. report background)
STYLE_FIELD_MAX = 100       # enum-ish UI values (length, tone, format, language)
HISTORY_MAX_TURNS = 50      # most-recent chat turns kept
HISTORY_FIELD_MAX = 2000    # chars kept per field within each turn
CODEBOOK_MAX = 100000       # serialized codebook budget (wide datasets)
PLOTS_MAX_COUNT = 3         # prompts only ever attach the first 3 plots
PLOT_B64_MAX = 2000000      # per-plot base64 payload cap (~1.5 MB decoded)


def clamp(text, limit):
    """Coerce to str and hard-truncate to ``limit`` characters."""
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    return text[:limit]


def clamp_history(turns, max_turns=HISTORY_MAX_TURNS,
                  field_max=HISTORY_FIELD_MAX):
    """Keep the most recent ``max_turns`` {role, text} turns, fields clamped.

    Non-list payloads and non-dict turns are dropped (the prompt formatters
    skip them anyway), so the result is always a bounded, well-shaped list.
    """
    if not isinstance(turns, list):
        return []
    clamped = []
    for turn in turns[-max_turns:]:
        if not isinstance(turn, dict):
            continue
        clamped.append({'role': clamp(turn.get('role') or 'user', 32),
                        'text': clamp(turn.get('text'), field_max)})
    return clamped


def clamp_codebook(codebook, limit=CODEBOOK_MAX):
    """Bound a {column: description} mapping to ~``limit`` serialized chars.

    Entries are kept in order until the budget is spent; everything after is
    dropped (the routes already degrade to headers-only context).
    """
    if not isinstance(codebook, dict):
        return {}
    kept, total = {}, 0
    for key, value in codebook.items():
        entry = len(str(key)) + len(json.dumps(value, default=str))
        if total + entry > limit:
            break
        kept[key] = value
        total += entry
    return kept


def clamp_plots(plots, max_count=PLOTS_MAX_COUNT, max_chars=PLOT_B64_MAX):
    """Cap plot attachments: at most ``max_count`` base64 strings, each within
    ``max_chars``. Oversized or non-string entries are dropped, not truncated,
    because a truncated base64 image is garbage anyway."""
    if not isinstance(plots, list):
        return []
    kept = []
    for plot in plots:
        if not isinstance(plot, str) or len(plot) > max_chars:
            continue
        kept.append(plot)
        if len(kept) >= max_count:
            break
    return kept


def sse_stream(generator):
    """Wrap a generator function in a standard Server-Sent Events response."""
    return Response(
        stream_with_context(generator()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def sse_event(payload):
    return f"data: {json.dumps(payload)}\n\n"


def strip_code_fences(text):
    """Defensive fence-stripping for models that ignore the no-markdown rule."""
    final_code = (text or '').strip()
    # Broadened fence tag (P2-6): match any language label (```py, ```python,
    # ```r, ```javascript, or a bare ```), not just python/r, so a mis-tagged
    # fence is stripped cleanly instead of falling through to the line-strip.
    match = re.search(r'```[a-zA-Z]*\n(.*?)```', final_code,
                      re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    if final_code.startswith('```'):
        return '\n'.join(
            line for line in final_code.split('\n')
            if not line.strip().startswith('```'))
    return final_code
