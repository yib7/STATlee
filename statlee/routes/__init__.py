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
    match = re.search(r'```(?:python|r)?\n(.*?)```', final_code,
                      re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    if final_code.startswith('```'):
        return '\n'.join(
            line for line in final_code.split('\n')
            if not line.strip().startswith('```'))
    return final_code
