"""Index, sample dataset, health, metrics (3.3) and issue reporting (6.3)."""
import hmac
import logging
import os
import threading
import time

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    session,
)

from .. import llm, mailer
from ..extensions import db, limiter
from ..identity import current_user_or_none
from . import (
    json_error,
    register_field_errors,
    request_json,
    str_field,
)

logger = logging.getLogger('statlee.misc')

bp = Blueprint('misc', __name__)
register_field_errors(bp)

_START_TIME = time.time()

# The synthetic sample survey the README points first-time users at
# (docs/examples/README.md). It ships in the repository and the Docker image,
# not in an installed wheel, so the upload area offers it only when present.
SAMPLE_DATASET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'docs', 'examples', 'sample_survey.csv')


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
        sample_available=os.path.isfile(SAMPLE_DATASET),
    )


@bp.route('/sample_survey.csv')
def sample_dataset():
    """Download the synthetic sample survey, so a first run has data to try.
    Behind the same sign-in gate as the app itself."""
    if not os.path.isfile(SAMPLE_DATASET):
        abort(404)
    return send_file(SAMPLE_DATASET, mimetype='text/csv', as_attachment=True,
                     download_name='sample_survey.csv')


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
