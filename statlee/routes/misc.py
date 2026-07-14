"""Index, health, metrics (3.3), issue reporting (6.3), report builder (5.17)."""
import logging
import time

from flask import Blueprint, current_app, jsonify, render_template, request, session

from .. import billing, llm, prompts
from ..extensions import db, limiter
from ..identity import current_user_or_none
from ..usage import usage_breakdown
from . import (
    BACKGROUND_MAX,
    FREE_TEXT_MAX,
    STYLE_FIELD_MAX,
    clamp,
    clamp_history,
    json_error,
    moderation_blocked,
    sse_event,
    sse_stream,
)

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
        # Code-generation model ids by role, so the generation header can name
        # the model actually doing the work ('pro_max' is the Pro-mode
        # upgrade). Display only: naming a model never selects or bills one.
        model_roles={'draft': cfg.model_pro, 'pro_max': cfg.model_pro_max},
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
    u = current_user_or_none()
    report = IssueReport(
        user_id=(u.id if u else None),
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
# AI report builder (5.17): generation and targeted revision, streamed
# ---------------------------------------------------------------------------

def _moderate_free_text(service, text):
    """P1-2: the same fail-closed moderation gate /converse applies, run on
    the report builder's client free-text. Returns ``(error_response, usage)``
    where ``error_response`` is the response to send when the text is blocked
    (or the service failed), else ``None``."""
    try:
        mod = service.generate('lite', prompts.moderation(text),
                               temperature=0.0, json_mode=True)
    except Exception:
        logger.exception("Report moderation failed")
        return json_error('Moderation service failed.', 503), None
    blocked, reason = moderation_blocked(mod.text)
    if blocked:
        return json_error(f'Request denied. {reason}', 403), None
    return None, mod.usage


@bp.route('/generate_report', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def generate_report():
    data = request.get_json(silent=True) or {}
    revision = data.get('revision')
    if revision is not None and not isinstance(revision, dict):
        return json_error("'revision' must be an object.")
    service = llm.get_service()
    cfg = _cfg()
    mod_usage = None
    debited = False
    billing_user = None

    if revision:
        # P1-7/P1-2: every revision field is client text; clamp before use.
        selection = clamp(revision.get('selection'), FREE_TEXT_MAX).strip()
        instruction = clamp(revision.get('instruction'), BACKGROUND_MAX).strip()
        if not selection or not instruction:
            return json_error('Revision needs a selected passage and an instruction.')
        # The instruction is free-form user text; moderate it like /converse.
        blocked_resp, mod_usage = _moderate_free_text(service, instruction)
        if blocked_resp is not None:
            return blocked_resp
        prompt = prompts.report_revision(
            clamp(revision.get('report'), FREE_TEXT_MAX), selection,
            instruction)
    else:
        # P1-7/P1-2: cap every client-supplied field before it can reach the
        # two paid model calls below.
        output = clamp(data.get('output'), FREE_TEXT_MAX).strip()
        interpretation = clamp(data.get('interpretation'), FREE_TEXT_MAX).strip()
        if not output and not interpretation:
            return json_error(
                'Run an analysis first: the report must be grounded in '
                'actual results.', 422)
        background = clamp(data.get('background'), BACKGROUND_MAX)
        # Style fields are enum-ish UI values but still client text that is
        # interpolated into both paid prompts; clamp (and stringify) them so
        # neither a multi-megabyte 'length' nor a non-string 'format' can
        # reach prompt construction.
        length = clamp(data.get('length'), STYLE_FIELD_MAX)
        tone = clamp(data.get('tone'), STYLE_FIELD_MAX)
        fmt = clamp(data.get('format'), STYLE_FIELD_MAX)
        history = clamp_history(data.get('history'))
        converse = clamp_history(data.get('converse'))

        # Background is the free-form user text on this path; moderate it
        # (fail-closed) before any billable work, mirroring /converse.
        if background.strip():
            blocked_resp, mod_usage = _moderate_free_text(service, background)
            if blocked_resp is not None:
                return blocked_resp

        # First pass runs on the bigger 3.1-pro model to compile a grounded
        # draft. Built BEFORE the debit (prompt construction is free) so no
        # exception here can ever strand a debited credit.
        draft_prompt = prompts.report_draft(
            background, length, fmt, output, interpretation, history, converse)

        # P1-2: the draft pass runs on the priciest (pro_max) tier, so it must
        # clear the same billing chokepoint as Pro-mode /chat, in the settled
        # moderate -> validate -> debit order. Without this the report builder
        # is a free tunnel to the tier billing exists to meter. Everything
        # after this point runs inside generate()'s try, whose failure path
        # refunds the credit.
        billing_user = current_user_or_none()
        allowed, deny_msg = billing.check_and_debit(
            billing_user, priority=True, config=cfg)
        if not allowed:
            return json_error(deny_msg or 'Out of credits.', 402)
        debited = True

    def generate():
        try:
            if revision:
                usage = {}
                for delta in service.stream('pro', prompt, temperature=0.4,
                                            usage_out=usage):
                    yield sse_event({'type': 'delta', 'text': delta})
                yield sse_event({'type': 'done',
                                 'usage': usage_breakdown(usage, mod_usage),
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
                                                      stream_usage, mod_usage),
                             'revision': False})
        except Exception:
            logger.exception("Report generation failed")
            if debited:
                # The credit was taken before any model call; the request
                # produced no report, so give it back (the same refund-on-
                # stream-failure contract /chat honors). Best-effort: a
                # refund failure must not mask the error the user sees.
                try:
                    billing.refund(billing_user, priority=True, config=cfg)
                except Exception:
                    logger.exception(
                        "Failed to refund credit after report failure")
            yield sse_event({'type': 'error',
                             'message': 'Report generation failed.'})

    return sse_stream(generate)
