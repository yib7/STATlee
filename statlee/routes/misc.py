"""Index, health, metrics (3.3), issue reporting (6.3), report builder (5.17)."""
import logging
import time

from flask import Blueprint, current_app, jsonify, render_template, request, session
from flask_login import current_user

from .. import llm, prompts
from ..extensions import db, limiter
from ..usage import usage_breakdown
from . import json_error, sse_event, sse_stream

logger = logging.getLogger('statlee.misc')

bp = Blueprint('misc', __name__)

_START_TIME = time.time()


def _cfg():
    return current_app.config['STATLEE']


@bp.route('/')
def index():
    cfg = current_app.config['STATLEE']
    return render_template(
        'index.html',
        csrf_token=session.get('csrf_token', ''),
        accounts_enabled=cfg.accounts_enabled,
        model_prices=cfg.active_model_prices(),
    )


@bp.route('/welcome')
def welcome():
    """Public marketing/landing page (workstream D). The app itself lives at
    '/'; this is the startup-style front door that links into it."""
    return render_template('landing.html')


@bp.route('/health')
def health_check():
    return 'OK', 200


@bp.route('/metrics')
def metrics():
    """Structured usage/uptime snapshot (3.3)."""
    try:
        usage = llm.get_service().usage_snapshot()
    except RuntimeError:
        usage = {}
    return jsonify({
        'uptime_seconds': int(time.time() - _START_TIME),
        'llm_usage': usage,
    })


# ---------------------------------------------------------------------------
# In-app issue reporting (6.3)
# ---------------------------------------------------------------------------

@bp.route('/report_issue', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def report_issue():
    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    if not description:
        return json_error('Please describe the issue.')

    from ..models import IssueReport
    report = IssueReport(
        user_id=(current_user.id
                 if current_user and getattr(current_user, 'is_authenticated', False)
                 else None),
        description=description[:10000],
        code=(data.get('code') or '')[:20000],
        output=(data.get('output') or '')[:20000],
        console_errors=(data.get('console_errors') or '')[:10000],
        user_agent=(request.headers.get('User-Agent') or '')[:512],
    )
    db.session.add(report)
    db.session.commit()
    logger.warning("ISSUE REPORT #%s: %s", report.id, description[:300])

    cfg = current_app.config['STATLEE']
    if cfg.smtp_host and cfg.issue_report_to:
        try:
            _email_report(cfg, report)
        except Exception:
            logger.exception("Failed to email issue report %s", report.id)

    return jsonify({'status': 'success', 'id': report.id})


def _email_report(cfg, report):
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg['Subject'] = f'[STATlee] Issue report #{report.id}'
    msg['From'] = cfg.smtp_user or 'statlee@localhost'
    msg['To'] = cfg.issue_report_to
    msg.set_content(
        f"Description:\n{report.description}\n\n"
        f"--- Active code ---\n{report.code or '(none)'}\n\n"
        f"--- Terminal output ---\n{report.output or '(none)'}\n\n"
        f"--- Console errors ---\n{report.console_errors or '(none)'}\n\n"
        f"User-Agent: {report.user_agent}")
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
        server.starttls()
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# AI report builder (5.17) — generation and targeted revision, streamed
# ---------------------------------------------------------------------------

@bp.route('/generate_report', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def generate_report():
    data = request.get_json(silent=True) or {}
    revision = data.get('revision')
    service = llm.get_service()

    if revision:
        selection = (revision.get('selection') or '').strip()
        instruction = (revision.get('instruction') or '').strip()
        if not selection or not instruction:
            return json_error('Revision needs a selected passage and an instruction.')
        prompt = prompts.report_revision(
            revision.get('report') or '', selection, instruction)
    else:
        output = (data.get('output') or '').strip()
        interpretation = (data.get('interpretation') or '').strip()
        if not output and not interpretation:
            return json_error(
                'Run an analysis first — the report must be grounded in '
                'actual results.', 422)
        background = data.get('background')
        length = data.get('length')
        tone = data.get('tone')
        fmt = data.get('format')
        history = data.get('history')
        converse = data.get('converse')
        # First pass runs on the bigger 3.1-pro model to compile a grounded draft.
        draft_prompt = prompts.report_draft(
            background, length, fmt, output, interpretation, history, converse)

    def generate():
        try:
            if revision:
                usage = {}
                for delta in service.stream('pro', prompt, temperature=0.4,
                                            usage_out=usage):
                    yield sse_event({'type': 'delta', 'text': delta})
                yield sse_event({'type': 'done', 'usage': usage_breakdown(usage),
                                 'revision': True})
                return
            # Two-pass authoring: 3.1-pro compiles all the material (results,
            # interpretation, and the converse discussion of the findings) into a
            # grounded working draft, then 3.5-flash writes the finished piece.
            draft_result = service.generate('pro_max', draft_prompt,
                                            temperature=0.3)
            final_prompt = prompts.report(
                background, length, tone, fmt, output, interpretation,
                history, draft=draft_result.text)
            stream_usage = {}
            for delta in service.stream('pro', final_prompt, temperature=0.4,
                                        usage_out=stream_usage):
                yield sse_event({'type': 'delta', 'text': delta})
            yield sse_event({'type': 'done',
                             'usage': usage_breakdown(draft_result.usage,
                                                      stream_usage),
                             'revision': False})
        except Exception:
            logger.exception("Report generation failed")
            yield sse_event({'type': 'error',
                             'message': 'Report generation failed.'})

    return sse_stream(generate)
