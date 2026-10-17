"""Index, health, metrics (3.3), issue reporting (6.3), report builder (5.17)."""
import hmac
import logging
import threading
import time

from flask import Blueprint, current_app, jsonify, render_template, request, session

from .. import billing, llm, mailer, prompts, storage
from .. import methods as methods_card
from ..extensions import db, limiter
from ..identity import current_user_or_none
from ..usage import usage_breakdown
from . import (
    BACKGROUND_MAX,
    FREE_TEXT_MAX,
    STYLE_FIELD_MAX,
    DebitGuard,
    FieldError,
    RunNotFound,
    clamp,
    clamp_history,
    json_error,
    llm_error_payload,
    moderation_gate,
    register_field_errors,
    request_json,
    sse_event,
    sse_stream,
    str_field,
)

logger = logging.getLogger('statlee.misc')

bp = Blueprint('misc', __name__)
register_field_errors(bp)

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


def _metrics_denied():
    """None when this request may read /metrics, else the response to send.

    The snapshot is operational data about every user of the server (uptime,
    per-model call and token totals), so it is an operator endpoint (S5): with
    METRICS_TOKEN set the request must carry that token, with or without a
    session; unset, production hides the route (404) and development/testing
    keep the normal app gate."""
    cfg = _cfg()
    if cfg.metrics_token:
        auth = request.headers.get('Authorization', '')
        supplied = (auth[7:].strip() if auth[:7].lower() == 'bearer '
                    else request.headers.get('X-Metrics-Token', '').strip())
        if supplied and hmac.compare_digest(
                supplied.encode('utf-8', 'replace'),
                cfg.metrics_token.encode('utf-8', 'replace')):
            return None
        resp = jsonify({'error': 'A valid metrics token is required.'})
        resp.headers['WWW-Authenticate'] = 'Bearer'
        return resp, 401
    if cfg.is_production:
        return json_error('Not found.', 404)
    from .auth import is_authorized
    if not is_authorized():
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    return None


@bp.route('/metrics')
def metrics():
    """Structured usage/uptime snapshot (3.3), for the operator only."""
    denied = _metrics_denied()
    if denied is not None:
        return denied
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

# B-16: /report_issue is open to anonymous callers, stores up to ~60 KB a row
# and emails the operator. The chat limit (20/min per IP) allowed ~28,800 rows
# and emails a day per IP, so it gets its own tight limit, the table keeps only
# the newest ISSUE_REPORTS_MAX_ROWS rows, and at most ISSUE_EMAILS_PER_HOUR
# reports per process are emailed (the rest are still stored and logged).
REPORT_ISSUE_RATE_COUNT = 5
REPORT_ISSUE_RATE = f'{REPORT_ISSUE_RATE_COUNT} per hour'
ISSUE_REPORTS_MAX_ROWS = 1000
ISSUE_EMAILS_PER_HOUR = 10
_email_times = []
_email_lock = threading.Lock()


def _email_allowed(now=None):
    """Consume one slot of the per-process hourly email budget."""
    now = time.time() if now is None else now
    with _email_lock:
        _email_times[:] = [t for t in _email_times if now - t < 3600]
        if len(_email_times) >= ISSUE_EMAILS_PER_HOUR:
            return False
        _email_times.append(now)
        return True


def _prune_issue_reports():
    """Keep only the newest ``ISSUE_REPORTS_MAX_ROWS`` reports."""
    from ..models import IssueReport
    keep_ids = db.session.execute(
        db.select(IssueReport.id).order_by(IssueReport.id.desc())
        .limit(ISSUE_REPORTS_MAX_ROWS)).scalars().all()
    db.session.execute(
        db.delete(IssueReport).where(IssueReport.id.not_in(keep_ids)))


@bp.route('/report_issue', methods=['POST'])
@limiter.limit(REPORT_ISSUE_RATE)
def report_issue():
    data = request_json()
    # D1-12: non-string fields are a 400, not a 500 from slicing a dict.
    description = str_field(data, 'description', 10000).strip()
    if not description:
        return json_error('Please describe the issue.')

    from ..models import IssueReport
    u = current_user_or_none()
    report = IssueReport(
        user_id=(u.id if u else None),
        description=description,
        code=str_field(data, 'code', 20000),
        output=str_field(data, 'output', 20000),
        console_errors=str_field(data, 'console_errors', 10000),
        user_agent=(request.headers.get('User-Agent') or '')[:512],
    )
    db.session.add(report)
    db.session.flush()
    _prune_issue_reports()
    db.session.commit()
    logger.warning("ISSUE REPORT #%s: %s", report.id, description[:300])

    cfg = current_app.config['STATLEE']
    if cfg.smtp_host and cfg.issue_report_to:
        if not _email_allowed():
            logger.warning("Issue report %s not emailed: more than %s reports "
                           "this hour", report.id, ISSUE_EMAILS_PER_HOUR)
            return jsonify({'status': 'success', 'id': report.id})
        try:
            _email_report(cfg, report)
        except Exception:
            logger.exception("Failed to email issue report %s", report.id)

    return jsonify({'status': 'success', 'id': report.id})


def _email_report(cfg, report):
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
    mailer.send(cfg, msg)


# ---------------------------------------------------------------------------
# AI report builder (5.17): generation and targeted revision, streamed
# ---------------------------------------------------------------------------

def _join_material(*sections):
    """Label and join the non-empty (title, text) client sections."""
    return '\n\n'.join(f'{title}:\n{text}' for title, text in sections
                        if text and text.strip()) or None


REPORT_RUN_IDS_MAX = 200        # far above any identity's run cap
REPORT_RUN_LABEL_MAX = 200      # chars of a run's question in its heading
REPORT_METHODS_MAX = 12000      # chars of all the runs' Methods cards


def _report_run_ids(data):
    """``(run_ids, explicit)`` for /generate_report (SP1). Without
    ``run_ids`` the report grounds on the latest run; with them, on each
    named run, in the order they were run. Every id must be one of the
    caller's own runs (``RunNotFound`` otherwise, never a silent skip)."""
    raw = data.get('run_ids')
    if raw is None or raw == []:
        latest = storage.latest_run_id()
        return ([latest] if latest else []), False
    if (not isinstance(raw, list) or len(raw) > REPORT_RUN_IDS_MAX
            or not all(isinstance(r, str) for r in raw)):
        raise FieldError("'run_ids' must be a list of run ids.")
    wanted = set(raw)
    for run_id in wanted:
        if storage.run_meta(run_id) is None:
            raise RunNotFound(run_id)
    return [r for r in storage.run_ids() if r in wanted], True


def _run_card(run_id, output):
    """The run's Methods card as prompt text (SP3): its stored card, or one
    parsed from its output for a run saved before SP3."""
    return methods_card.render_text(
        methods_card.for_run(storage.run_meta(run_id), output))


def _run_grounding(run_ids):
    """``(output, interpretation, methods)`` from the server's record of the
    selected runs. One run reads exactly as before; several are each
    labelled with their id and question (both server-side: the question
    passed /chat's moderation; client-chosen run names are left out of the
    prompt) and share the same ``FREE_TEXT_MAX`` budget evenly. ``methods``
    is each run's Methods card (SP3), in its own ``REPORT_METHODS_MAX``
    budget, labelled the same way."""
    if len(run_ids) == 1:
        output, _plots = storage.run_artifacts(run_ids[0])
        return (clamp(output, FREE_TEXT_MAX).strip(),
                clamp(storage.run_interpretation(run_ids[0]),
                      FREE_TEXT_MAX).strip(),
                clamp(_run_card(run_ids[0], output),
                      REPORT_METHODS_MAX).strip())
    share = FREE_TEXT_MAX // max(1, len(run_ids))
    card_share = REPORT_METHODS_MAX // max(1, len(run_ids))
    outputs, interps, cards = [], [], []
    for run_id in run_ids:
        question = clamp((storage.run_meta(run_id) or {}).get('prompt'),
                         REPORT_RUN_LABEL_MAX).strip()
        heading = f'=== Run {run_id}: {question} ===' if question \
            else f'=== Run {run_id} ==='
        body_max = max(0, share - len(heading) - 2)
        raw_output = storage.run_artifacts(run_id)[0]
        output = clamp(raw_output, body_max).strip()
        interp = clamp(storage.run_interpretation(run_id), body_max).strip()
        card = clamp(_run_card(run_id, raw_output),
                     max(0, card_share - len(heading) - 2)).strip()
        if output:
            outputs.append(f'{heading}\n{output}')
        if interp:
            interps.append(f'{heading}\n{interp}')
        if card:
            cards.append(f'{heading}\n{card}')
    return ('\n\n'.join(outputs)[:FREE_TEXT_MAX],
            '\n\n'.join(interps)[:FREE_TEXT_MAX],
            '\n\n'.join(cards)[:REPORT_METHODS_MAX])


def _moderate_free_text(service, text, material=None):
    """P1-2: the same fail-closed moderation gate /converse applies, run on
    the report builder's client free-text. Returns ``(error_response, usage)``
    where ``error_response`` is the response to send when the text is blocked
    (or the service failed), else ``None``."""
    denied, mod = moderation_gate(service, prompts.moderation(text, material),
                                  log_message="Report moderation failed")
    if denied is not None:
        return denied, None
    return None, mod.usage


@bp.route('/generate_report', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def generate_report():
    data = request_json()
    revision = data.get('revision')
    if revision is not None and not isinstance(revision, dict):
        return json_error("'revision' must be an object.")
    service = llm.get_service()
    cfg = _cfg()
    mod_usage = None
    debited = False
    billing_user = None
    run_ids = []

    if revision:
        # P1-7/P1-2: every revision field is client text; clamp before use.
        selection = clamp(revision.get('selection'), FREE_TEXT_MAX).strip()
        instruction = clamp(revision.get('instruction'), BACKGROUND_MAX).strip()
        if not selection or not instruction:
            return json_error('Revision needs a selected passage and an instruction.')
        # The instruction is free-form user text; moderate it like /converse.
        # D3-2: the report and the selection are client text too and reach
        # the model, so they are reviewed with it.
        blocked_resp, mod_usage = _moderate_free_text(
            service, instruction,
            material=_join_material(
                ('SELECTED PASSAGE', selection),
                ('REPORT', clamp(revision.get('report'), FREE_TEXT_MAX))))
        if blocked_resp is not None:
            return blocked_resp
        prompt = prompts.report_revision(
            clamp(revision.get('report'), FREE_TEXT_MAX), selection,
            instruction)
    else:
        # P1-7/P1-2: cap every client-supplied field before it can reach the
        # two paid model calls below.
        # D3-2: ground on the server's record of the last run (its output and
        # the interpretation /interpret stored) and fall back to the client's
        # copy only where no server record exists. Client copies are then
        # moderated below with the rest of the client text.
        # SP1: ``run_ids`` selects the runs (default: the latest). Named runs
        # are grounded on the server's record only; the client copies are a
        # fallback for the default path with no server run.
        # SP3: each grounded run's Methods card goes in too, from the
        # server's record only (never a client field).
        run_ids, explicit = _report_run_ids(data)
        server_output, server_interp, cards = (
            _run_grounding(run_ids) if run_ids else ('', '', ''))
        if not (server_output or explicit):
            cards = ''          # the client-copy fallback has no server run
        if server_output or explicit:
            output, output_trusted = server_output, True
        else:
            output = clamp(data.get('output'), FREE_TEXT_MAX).strip()
            output_trusted = False
        if server_interp or explicit:
            interpretation, interp_trusted = server_interp, True
        else:
            interpretation = clamp(data.get('interpretation'),
                                   FREE_TEXT_MAX).strip()
            interp_trusted = False
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

        # Every piece of client text that reaches the two paid calls (the
        # background, unverified results, the chat and converse history) is
        # moderated together (fail-closed) before any billable work (D3-2).
        material = _join_material(
            ('TERMINAL OUTPUT', '' if output_trusted else output),
            ('INTERPRETATION', '' if interp_trusted else interpretation),
            ('ANALYSIS HISTORY', prompts._format_turns(history)),
            ('DISCUSSION', prompts._format_turns(converse)))
        if background.strip() or material:
            blocked_resp, mod_usage = _moderate_free_text(
                service, background.strip() or '(write a report on my analysis)',
                material=material)
            if blocked_resp is not None:
                return blocked_resp

        # First pass runs on the bigger 'pro_max' model to compile a grounded
        # draft. Built BEFORE the debit (prompt construction is free) so no
        # exception here can ever strand a debited credit.
        draft_prompt = prompts.report_draft(
            background, length, fmt, output, interpretation, history, converse,
            methods_card=cards or None)

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

    debit = DebitGuard(billing_user, priority=True, config=cfg,
                       debited=debited)

    def generate():
        try:
            if revision:
                usage = {}
                for delta in service.stream('pro', prompt, temperature=0.4,
                                            usage_out=usage):
                    yield sse_event({'type': 'delta', 'text': delta})
                debit.delivered()
                yield sse_event(debit.with_balance({
                    'type': 'done',
                    'usage': usage_breakdown(usage, mod_usage),
                    'revision': True}))
                return
            # Two-pass authoring: the 'pro_max' model compiles all the material
            # (results, interpretation, and the converse discussion of the
            # findings) into a grounded working draft, then the default 'pro'
            # model writes the finished piece. B-14: the draft is streamed
            # (and collected here, not shown): streams get the larger output
            # budget, which a thinking Pro model needs, and no non-streaming
            # request timeout applies to a long draft.
            draft_usage = {}
            draft_text = ''.join(service.stream(
                'pro_max', draft_prompt, temperature=0.3,
                usage_out=draft_usage))
            final_prompt = prompts.report(
                background, length, tone, fmt, output, interpretation,
                history, draft=draft_text, methods_card=cards or None)
            stream_usage = {}
            for delta in service.stream('pro', final_prompt, temperature=0.4,
                                        usage_out=stream_usage):
                yield sse_event({'type': 'delta', 'text': delta})
            debit.delivered()
            yield sse_event(debit.with_balance({
                'type': 'done',
                'usage': usage_breakdown(draft_usage,
                                         stream_usage, mod_usage),
                'revision': False,
                'run_ids': run_ids}))
        except Exception as exc:
            logger.exception("Report generation failed")
            # The credit was taken before any model call; the request
            # produced no report, so give it back (the same refund-on-
            # stream-failure contract /chat honors). Best-effort: a
            # refund failure must not mask the error the user sees.
            debit.refund()
            yield sse_event(debit.with_balance(
                llm_error_payload(exc, 'Report generation failed.')))
        finally:
            debit.refund()   # D1-15: client disconnect (GeneratorExit)

    return debit.attach(sse_stream(generate))
