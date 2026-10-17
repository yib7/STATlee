"""Converse tab routes.

Single-pass: one moderated, streamed model call with persona guardrails and
formatting rules inlined, so there is no separate formatting call.
``mode='guide'`` switches to the hypothesis-coach persona.
"""
import logging

from flask import Blueprint, current_app

from .. import llm, prompts, storage
from ..extensions import limiter
from ..usage import usage_breakdown
from . import (
    FREE_TEXT_MAX,
    clamp,
    clamp_history,
    json_error,
    llm_error_payload,
    moderation_gate,
    register_field_errors,
    request_json,
    run_id_field,
    selected_run_id,
    sse_event,
    sse_stream,
)

logger = logging.getLogger('statlee.converse')

bp = Blueprint('converse', __name__)
register_field_errors(bp)


def _cfg():
    return current_app.config['STATLEE']


def _known_script(code, run_id=None):
    """True when ``code`` is a script the server already reviewed: approved
    for either language, or the script that produced the selected run."""
    if any(storage.is_approved_script(code, lang) for lang in ('Python', 'R')):
        return True
    record = storage.run_script(run_id) if run_id else None
    return bool(record and record.get('code') == code)


@bp.route('/converse', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def converse():
    data = request_json()
    # Clamp every client field that flows into the prompt (including
    # the moderated message) so a single request cannot smuggle megabytes of
    # input tokens onto the operator's key.
    message = clamp(data.get('message'), FREE_TEXT_MAX).strip()
    history = clamp_history(data.get('history', []))
    context = clamp(data.get('context'), FREE_TEXT_MAX)
    code = clamp(data.get('code'), FREE_TEXT_MAX)
    guide_mode = data.get('mode') == 'guide'
    # The run being discussed (default: the latest); a named run the
    # caller does not have is a 404.
    run_id = selected_run_id(run_id_field(data))

    if not message:
        return json_error('Missing message')

    service = llm.get_service()
    cfg = current_app.config['STATLEE']

    # The same baseline moderation gate /chat uses (structured + default-deny).
    # Ground on the server's records where they exist: the stored
    # interpretation of the last run replaces the client's `context`, and a
    # script the run-guard already approved (or that produced the last run)
    # needs no second review. Whatever client text remains (an unverified
    # context, an edited or pasted script, the chat history) is moderated
    # together with the message, failing closed.
    server_context = (storage.run_interpretation(run_id).strip()
                      if run_id else '')
    if server_context:
        context = clamp(server_context, FREE_TEXT_MAX)
    unverified = []
    if not server_context and context.strip():
        unverified.append('RESULTS CONTEXT:\n' + context)
    if code.strip() and not _known_script(code, run_id):
        unverified.append('SCRIPT:\n' + code)
    history_text = '\n'.join(t['text'] for t in history if t.get('text'))
    if history_text.strip():
        unverified.append('CONVERSATION HISTORY:\n' + history_text)

    denied, mod = moderation_gate(
        service, prompts.moderation(message, '\n\n'.join(unverified) or None),
        log_message="Converse moderation failed")
    if denied is not None:
        return denied

    prompt = prompts.converse(message, history, context, code,
                              guide_mode=guide_mode)

    def generate():
        usage = {}
        try:
            for delta in service.stream(cfg.converse_role, prompt,
                                        temperature=0.6, usage_out=usage):
                yield sse_event({'type': 'delta', 'text': delta})
            done_usage = usage_breakdown(usage, mod.usage)
            yield sse_event({'type': 'done', 'usage': done_usage,
                             'run_id': run_id})
        except Exception as exc:
            logger.exception("Converse stream failed")
            yield sse_event(llm_error_payload(
                exc, 'Failed to generate a response.'))

    return sse_stream(generate)
