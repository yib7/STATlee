"""Authentication & account routes (roadmap 7.1) + per-user history (5.7/7.2).

Three coexisting modes keep the anonymous sandbox promise intact:
- open:      no PASSWORD set, no login required — anyone can use the sandbox.
- password:  legacy single APP_PASSWORD gate (backwards compatible).
- accounts:  optional email/password accounts (Flask-Login). REQUIRE_LOGIN=true
             makes them mandatory.
"""
import logging
import re

from flask import Blueprint, current_app, jsonify, request, session
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
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    logger.info("New account registered: %s (id=%s)", email, user.id)
    return jsonify({'status': 'success', 'user': {'email': user.email}}), 201


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
        prompt=data.get('prompt'),
        code=data.get('code'),
        output=data.get('output'),
        interpretation=data.get('interpretation'),
    )
    db.session.add(run)
    db.session.commit()
    return jsonify({'saved': True, 'id': run.id})
