"""Converse tab routes (roadmap 0.6 + 4.2 + 5.15).

Single-pass: one moderated, streamed model call with persona guardrails and
formatting rules inlined — the old second "formatting" LLM call is gone.
``mode='guide'`` switches to the hypothesis-coach persona.
"""
import logging

from flask import Blueprint, current_app, request

from .. import llm, prompts
from ..extensions import limiter
from ..usage import usage_breakdown
from . import (
    FREE_TEXT_MAX,
    clamp,
    clamp_history,
    json_error,
    moderation_blocked,
    sse_event,
    sse_stream,
)

logger = logging.getLogger('statlee.converse')

bp = Blueprint('converse', __name__)


def _cfg():
    return current_app.config['STATLEE']


@bp.route('/converse', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def converse():
    data = request.get_json(silent=True) or {}
    # P1-7: clamp every client field that flows into the prompt (including
    # the moderated message) so a single request cannot smuggle megabytes of
    # input tokens onto the operator's key.
    message = clamp(data.get('message'), FREE_TEXT_MAX).strip()
    history = clamp_history(data.get('history', []))
    context = clamp(data.get('context'), FREE_TEXT_MAX)
    code = clamp(data.get('code'), FREE_TEXT_MAX)
    guide_mode = data.get('mode') == 'guide'

    if not message:
        return json_error('Missing message')

    service = llm.get_service()
    cfg = current_app.config['STATLEE']

    # 0.6: the same baseline moderation gate /chat uses (structured + default-deny).
    try:
        mod = service.generate('lite', prompts.moderation(message),
                               temperature=0.0, json_mode=True)
    except Exception:
        logger.exception("Converse moderation failed")
        return json_error('Moderation service failed.', 503)
    blocked, reason = moderation_blocked(mod.text)
    if blocked:
        return json_error(f'Request denied. {reason}', 403)

    prompt = prompts.converse(message, history, context, code,
                              guide_mode=guide_mode)

    def generate():
        usage = {}
        try:
            for delta in service.stream(cfg.converse_role, prompt,
                                        temperature=0.6, usage_out=usage):
                yield sse_event({'type': 'delta', 'text': delta})
            done_usage = usage_breakdown(usage, mod.usage)
            yield sse_event({'type': 'done', 'usage': done_usage})
        except Exception:
            logger.exception("Converse stream failed")
            yield sse_event({'type': 'error',
                             'message': 'Failed to generate a response.'})

    return sse_stream(generate)
