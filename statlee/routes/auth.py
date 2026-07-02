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

from flask import Blueprint, current_app, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_user, logout_user

from ..extensions import db
from ..models import AnalysisRun, User
from . import json_error

logger = logging.getLogger('statlee.auth')

bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _cfg():
    return current_app.config['STATLEE']


def is_authorized():
    """Single authorization decision used by the app-level gate."""
    cfg = _cfg()
    if current_user and getattr(current_user, 'is_authenticated', False):
        # A logged-in account is only authorized once its email is confirmed,
        # when verification is required.
        if (cfg.require_email_verification
                and not getattr(current_user, 'email_verified', False)):
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
    if current_user and getattr(current_user, 'is_authenticated', False):
        user = {'email': current_user.email,
                'plan': current_user.plan, 'credits': current_user.credits}
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
    if data.get('password') == cfg.app_password:
        session['authenticated'] = True
        return jsonify({'status': 'success'}), 200
    return json_error('Invalid password', 401)


@bp.route('/register', methods=['POST'])
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
    else:
        user.email_verified = True
    db.session.add(user)
    try:
        db.session.commit()
    except Exception:
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


@bp.route('/verify_email', methods=['GET'])
def verify_email():
    """Confirm an account from the emailed link, then log the user in."""
    token = (request.args.get('token') or '').strip()
    if not token:
        return json_error('Missing verification token.', 400)
    user = db.session.execute(
        db.select(User).filter_by(verification_token=token)).scalar_one_or_none()
    if not user:
        return json_error('Invalid or expired verification link.', 400)
    user.email_verified = True
    user.verification_token = None
    db.session.commit()
    login_user(user, remember=True)
    logger.info("Email verified for %s (id=%s)", user.email, user.id)
    return redirect('/')


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
    if not (current_user and getattr(current_user, 'is_authenticated', False)):
        return jsonify({'runs': [], 'persisted': False})
    runs = db.session.execute(
        db.select(AnalysisRun)
        .filter_by(user_id=current_user.id)
        .order_by(AnalysisRun.created_at.desc())
        .limit(50)).scalars().all()
    return jsonify({'runs': [r.to_dict() for r in runs], 'persisted': True})


@bp.route('/history', methods=['POST'])
def history_save():
    if not (current_user and getattr(current_user, 'is_authenticated', False)):
        # Anonymous sessions keep history client-side only ("data not stored").
        return jsonify({'saved': False})
    data = request.get_json(silent=True) or {}
    run = AnalysisRun(
        user_id=current_user.id,
        dataset_name=(data.get('dataset_name') or '')[:255],
        language=(data.get('language') or 'Python')[:16],
        prompt=(data.get('prompt') or '')[:10000],
        code=(data.get('code') or '')[:20000],
        output=(data.get('output') or '')[:20000],
        interpretation=(data.get('interpretation') or '')[:20000],
    )
    db.session.add(run)
    db.session.commit()
    return jsonify({'saved': True, 'id': run.id})
