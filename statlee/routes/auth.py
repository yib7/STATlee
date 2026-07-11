"""Authentication & account routes (roadmap 7.1) + per-user history (5.7/7.2).

Three coexisting modes keep the anonymous sandbox promise intact:
- open:      no PASSWORD set, no login required — anyone can use the sandbox.
- password:  legacy single APP_PASSWORD gate (backwards compatible).
- accounts:  optional email/password accounts (Flask-Login). REQUIRE_LOGIN=true
             makes them mandatory.
"""
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import login_user, logout_user
from sqlalchemy.exc import IntegrityError

from ..extensions import db, limiter
from ..identity import current_user_or_none
from ..models import AnalysisRun, User
from . import json_error

logger = logging.getLogger('statlee.auth')

bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Hard cap on persisted history rows per user (P2-13). Field sizes are already
# capped in history_save; this bounds the row count too, so a looping client
# cannot grow the table without limit. Saves past the cap prune the oldest
# rows in the same transaction.
HISTORY_MAX_ROWS = 200

# Verification tokens expire after this window (P2-12); a stale or leaked link
# must not grant login forever.
VERIFY_TOKEN_TTL = timedelta(hours=48)

# Password-reset tokens are short-lived (P2-11): a reset link should be usable
# for one sitting, not indefinitely.
RESET_TOKEN_TTL = timedelta(hours=1)


def _token_age_ok(issued_at, ttl):
    """True when ``issued_at`` exists and is within ``ttl`` of now.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns,
    so normalize to aware UTC before comparing. A missing timestamp (legacy row)
    is treated as expired: fail closed."""
    if issued_at is None:
        return False
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - issued_at <= ttl


def _cfg():
    return current_app.config['STATLEE']


def is_authorized():
    """Single authorization decision used by the app-level gate."""
    cfg = _cfg()
    u = current_user_or_none()
    if u:
        # A logged-in account is only authorized once its email is confirmed,
        # when verification is required.
        if (cfg.require_email_verification
                and not getattr(u, 'email_verified', False)):
            return False
        return True
    if cfg.require_login:
        return False
    if cfg.app_password:
        return bool(session.get('authenticated'))
    return True


@bp.route('/check_auth', methods=['GET'])
def check_auth():
    cfg = _cfg()
    mode = 'accounts' if cfg.require_login else (
        'password' if cfg.app_password else 'open')
    user = None
    u = current_user_or_none()
    if u:
        user = {'email': u.email, 'plan': u.plan, 'credits': u.credits}
    if is_authorized():
        return jsonify({
            'status': 'authorized', 'mode': mode, 'user': user,
            'accounts_enabled': cfg.accounts_enabled,
        }), 200
    return jsonify({
        'error': 'unauthorized', 'mode': mode,
        'accounts_enabled': cfg.accounts_enabled,
    }), 401


@bp.route('/login', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_auth)
def login():
    cfg = _cfg()
    data = request.get_json(silent=True) or {}

    email = (data.get('email') or '').strip().lower()
    if email:
        if not cfg.accounts_enabled:
            return json_error('Accounts are disabled on this server.', 403)
        user = db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one_or_none()
        if user and user.check_password(data.get('password') or ''):
            if cfg.require_email_verification and not user.email_verified:
                return json_error(
                    'Please confirm your email before logging in. Check your '
                    'inbox for the verification link.', 403)
            login_user(user, remember=True)
            return jsonify({'status': 'success', 'user': {'email': user.email}}), 200
        return json_error('Invalid email or password.', 401)

    # Legacy master-password mode
    if not cfg.app_password:
        return jsonify({'status': 'success'}), 200
    if secrets.compare_digest(str(data.get('password') or ''), str(cfg.app_password)):
        session['authenticated'] = True
        return jsonify({'status': 'success'}), 200
    return json_error('Invalid password', 401)


@bp.route('/register', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_auth)
def register():
    cfg = _cfg()
    if not cfg.accounts_enabled:
        return json_error('Accounts are disabled on this server.', 403)
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not EMAIL_RE.match(email):
        return json_error('Please provide a valid email address.', 400)
    if len(password) < 8:
        return json_error('Password must be at least 8 characters.', 400)
    existing = db.session.execute(
        db.select(User).filter_by(email=email)).scalar_one_or_none()
    if existing:
        return json_error('An account with that email already exists.', 409)

    user = User(email=email)
    user.set_password(password)
    if cfg.require_email_verification:
        user.email_verified = False
        user.verification_token = secrets.token_urlsafe(32)
        user.token_issued_at = datetime.now(UTC)
    else:
        user.email_verified = True
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return json_error('An account with that email already exists.', 409)
    logger.info("New account registered: %s (id=%s)", email, user.id)

    if cfg.require_email_verification:
        try:
            _send_verification_email(cfg, user.email, user.verification_token)
        except Exception:
            logger.exception("Failed to send verification email to %s", email)
            return jsonify({
                'status': 'verification_email_failed',
                'message': 'Account created, but the verification email could not '
                           'be sent. Please contact the site operator.',
            }), 202
        return jsonify({
            'status': 'verification_required',
            'message': 'Account created. Check your email to confirm it before '
                       'logging in.',
        }), 202

    login_user(user, remember=True)
    return jsonify({'status': 'success', 'user': {'email': user.email}}), 201


def _send_verification_email(cfg, email, token):
    """Send the confirmation link, or log it when SMTP isn't configured (dev)."""
    link = url_for('auth.verify_email', token=token, _external=True)
    if not cfg.smtp_host:
        logger.info("Email verification link for %s (no SMTP configured): %s",
                    email, link)
        return
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg['Subject'] = '[STATlee] Confirm your email'
    msg['From'] = cfg.smtp_user or 'statlee@localhost'
    msg['To'] = email
    msg.set_content(
        "Welcome to STATlee. Confirm your account by opening this link:\n\n"
        f"{link}\n\nIf you didn't create this account, you can ignore this email.")
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
        server.starttls()
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


def _send_password_reset_email(cfg, email, token):
    """Send the reset link, or log it when SMTP isn't configured (dev).

    Mirrors ``_send_verification_email`` so the reset flow reuses the exact same
    SMTP plumbing (P2-11)."""
    link = url_for('auth.reset_password', token=token, _external=True)
    if not cfg.smtp_host:
        logger.info("Password reset link for %s (no SMTP configured): %s",
                    email, link)
        return
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg['Subject'] = '[STATlee] Reset your password'
    msg['From'] = cfg.smtp_user or 'statlee@localhost'
    msg['To'] = email
    msg.set_content(
        "A password reset was requested for your STATlee account. Open this "
        "link within one hour to choose a new password:\n\n"
        f"{link}\n\nIf you didn't request this, you can ignore this email; "
        "your password stays unchanged.")
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
        server.starttls()
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


@bp.route('/verify_email', methods=['GET'])
@limiter.limit(lambda: _cfg().rate_limit_verify)
def verify_email():
    """Confirm an account from the emailed link, then log the user in."""
    token = (request.args.get('token') or '').strip()
    if not token:
        return json_error('Missing verification token.', 400)
    user = db.session.execute(
        db.select(User).filter_by(verification_token=token)).scalar_one_or_none()
    if not user:
        return json_error('Invalid or expired verification link.', 400)
    # P2-12: reject a token older than the TTL. It is consumed either way below
    # (cleared on success); an expired one just does not confirm the account,
    # so the same generic message covers "unknown" and "too old".
    if not _token_age_ok(user.token_issued_at, VERIFY_TOKEN_TTL):
        user.verification_token = None
        user.token_issued_at = None
        db.session.commit()
        return json_error('Invalid or expired verification link.', 400)
    user.email_verified = True
    user.verification_token = None
    user.token_issued_at = None
    db.session.commit()
    login_user(user, remember=True)
    logger.info("Email verified for %s (id=%s)", user.email, user.id)
    return redirect('/')


# ---------------------------------------------------------------------------
# Password reset (P2-11) - mirrors the verify_email token machinery.
# ---------------------------------------------------------------------------

# Always-200 body for /request_password_reset. Constant so the response is
# byte-identical whether or not the email maps to a real account (no
# enumeration).
_RESET_REQUESTED_MSG = ('If that email is registered, a reset link has been '
                        'sent. Check your inbox.')


@bp.route('/request_password_reset', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_auth)
def request_password_reset():
    """Start a password reset. Always returns 200 with a generic message so a
    caller cannot tell whether the email exists (no user enumeration). If it
    does exist, mint a 1h token and email the reset link."""
    cfg = _cfg()
    if not cfg.accounts_enabled:
        return json_error('Accounts are disabled on this server.', 403)
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    generic = jsonify({'status': 'ok', 'message': _RESET_REQUESTED_MSG})
    if not EMAIL_RE.match(email):
        # Don't leak which addresses are even well-formed vs registered.
        return generic, 200

    user = db.session.execute(
        db.select(User).filter_by(email=email)).scalar_one_or_none()
    if user is None:
        return generic, 200

    user.password_reset_token = secrets.token_urlsafe(32)
    user.reset_token_issued_at = datetime.now(UTC)
    db.session.commit()
    try:
        _send_password_reset_email(cfg, user.email, user.password_reset_token)
    except Exception:
        logger.exception("Failed to send password reset email to %s", email)
        # Still return the generic 200: revealing send failures would leak
        # which addresses are registered.
    return generic, 200


def _reset_user_for_token(token):
    """Resolve a live (non-expired) reset token to its user, or None."""
    if not token:
        return None
    user = db.session.execute(
        db.select(User).filter_by(
            password_reset_token=token)).scalar_one_or_none()
    if user is None or not _token_age_ok(user.reset_token_issued_at,
                                         RESET_TOKEN_TTL):
        return None
    return user


@bp.route('/reset_password', methods=['GET', 'POST'])
@limiter.limit(lambda: _cfg().rate_limit_auth)
def reset_password():
    """GET validates the token and shows a minimal set-new-password form; POST
    applies the new password, then clears the token (single use, 1h expiry)."""
    if request.method == 'GET':
        token = (request.args.get('token') or '').strip()
        user = _reset_user_for_token(token)
        return render_template(
            'reset_password.html',
            token=token,
            valid=user is not None,
            csrf_token=session.get('csrf_token', '')), (200 if user else 400)

    # POST: token may arrive as JSON (API) or a posted form field (the page).
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or request.form.get('token') or '').strip()
    password = data.get('password') or request.form.get('password') or ''

    user = _reset_user_for_token(token)
    if user is None:
        return json_error('This reset link is invalid or has expired. '
                          'Request a new one.', 400)
    if len(password) < 8:
        return json_error('Password must be at least 8 characters.', 400)

    user.set_password(password)
    user.password_reset_token = None
    user.reset_token_issued_at = None
    db.session.commit()
    logger.info("Password reset completed for %s (id=%s)", user.email, user.id)
    return jsonify({'status': 'success',
                    'message': 'Your password has been reset. You can log in '
                               'with your new password.'}), 200


@bp.route('/logout', methods=['POST'])
def logout():
    logout_user()
    session.pop('authenticated', None)
    return jsonify({'status': 'success'}), 200


# ---------------------------------------------------------------------------
# Per-user analysis history (5.7 server side / 7.2)
# ---------------------------------------------------------------------------

@bp.route('/history', methods=['GET'])
def history_list():
    u = current_user_or_none()
    if not u:
        return jsonify({'runs': [], 'persisted': False})
    runs = db.session.execute(
        db.select(AnalysisRun)
        .filter_by(user_id=u.id)
        .order_by(AnalysisRun.created_at.desc())
        .limit(50)).scalars().all()
    return jsonify({'runs': [r.to_dict() for r in runs], 'persisted': True})


@bp.route('/history', methods=['POST'])
def history_save():
    u = current_user_or_none()
    if not u:
        # Anonymous sessions keep history client-side only ("data not stored").
        return jsonify({'saved': False})
    data = request.get_json(silent=True) or {}
    run = AnalysisRun(
        user_id=u.id,
        dataset_name=(data.get('dataset_name') or '')[:255],
        language=(data.get('language') or 'Python')[:16],
        prompt=(data.get('prompt') or '')[:10000],
        code=(data.get('code') or '')[:20000],
        output=(data.get('output') or '')[:20000],
        interpretation=(data.get('interpretation') or '')[:20000],
    )
    db.session.add(run)
    db.session.flush()
    # Keep only the newest HISTORY_MAX_ROWS rows for this user (P2-13); the
    # prune rides the same transaction as the insert.
    keep_ids = db.session.execute(
        db.select(AnalysisRun.id)
        .filter_by(user_id=u.id)
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
        .limit(HISTORY_MAX_ROWS)).scalars().all()
    db.session.execute(
        db.delete(AnalysisRun)
        .where(AnalysisRun.user_id == u.id,
               AnalysisRun.id.not_in(keep_ids)))
    db.session.commit()
    return jsonify({'saved': True, 'id': run.id})
