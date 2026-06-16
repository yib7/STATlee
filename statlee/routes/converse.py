"""Converse tab routes (roadmap 0.6 + 4.2 + 5.15).

Single-pass: one moderated, streamed model call with persona guardrails and
formatting rules inlined — the old second "formatting" LLM call is gone.
``mode='guide'`` switches to the hypothesis-coach persona.
"""
import logging

from flask import Blueprint, current_app, request

from .. import llm, prompts
from ..extensions import limiter
from . import json_error, sse_event, sse_stream

logger = logging.getLogger('statlee.converse')

bp = Blueprint('converse', __name__)


def _cfg():
    return current_app.config['STATLEE']


@bp.route('/converse', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def converse():
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    history = data.get('history', [])
    context = data.get('context', '')
    code = data.get('code', '')
    guide_mode = data.get('mode') == 'guide'

    if not message:
        return json_error('Missing message')

    service = llm.get_service()
    cfg = current_app.config['STATLEE']

    # 0.6: the same baseline moderation gate /chat uses.
    try:
        mod = service.generate('lite', prompts.moderation(message),
                               temperature=0.0)
        if 'BLOCK' in mod.text:
            return json_error(f'Request denied. {mod.text.strip()}', 403)
    except Exception:
        logger.exception("Converse moderation failed")
        return json_error('Moderation service failed.', 503)

    prompt = prompts.converse(message, history, context, code,
                              guide_mode=guide_mode)

    def generate():
        usage = {}
        try:
            for delta in service.stream(cfg.converse_role, prompt,
                                        temperature=0.6, usage_out=usage):
                yield sse_event({'type': 'delta', 'text': delta})
            done_usage = {
                'input': usage.get('input', 0) + mod.usage.get('input', 0),
                'output': usage.get('output', 0) + mod.usage.get('output', 0),
                'calls': 2,
            }
            yield sse_event({'type': 'done', 'usage': done_usage})
        except Exception:
            logger.exception("Converse stream failed")
            yield sse_event({'type': 'error',
                             'message': 'Failed to generate a response.'})

    return sse_stream(generate)
