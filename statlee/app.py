"""STATlee application factory.

The old 940-line monolith now lives in focused modules (roadmap 3.1):
config.py, storage.py, sandbox.py, llm.py, prompts.py, datatools.py,
models.py, and the routes/ blueprints. This file wires them together and
owns the cross-cutting middleware:

- per-session ids + per-identity file isolation (1.1)
- CSRF double-submit tokens + hardened session cookies (1.5)
- rate limiting (1.4)
- generic client errors / full server-side logs (1.6)
- request-id correlated logging (3.3)
"""
import logging
import os
import secrets
import uuid

from dotenv import load_dotenv
from flask import Flask, g, has_request_context, jsonify, request, session

from . import llm
from .config import Config
from .extensions import db, limiter, login_manager

load_dotenv()


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = (
            g.get('request_id', '-') if has_request_context() else '-')
        return True


def _setup_logging():
    root = logging.getLogger()
    if any(isinstance(f, RequestIdFilter)
           for h in root.handlers for f in h.filters):
        return
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s [%(request_id)s]: %(message)s')
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdFilter())


logger = logging.getLogger('statlee')

# Endpoints reachable without authorization (frontend loader, auth handshake).
PUBLIC_ENDPOINTS = {
    'misc.index', 'misc.welcome', 'static', 'misc.health_check',
    'auth.check_auth', 'auth.login', 'auth.register', 'auth.logout',
}


def create_app(config=None):
    _setup_logging()
    cfg = config or Config.from_env()

    app = Flask(__name__, template_folder='templates', static_folder='static')

    # Behind a trusted reverse proxy (e.g. Render), honour X-Forwarded-For so
    # rate limiting and logging see the real client IP rather than the proxy's.
    if cfg.trust_proxy_hops:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=cfg.trust_proxy_hops,
            x_proto=cfg.trust_proxy_hops)

    app.config['STATLEE'] = cfg
    app.config['MAX_CONTENT_LENGTH'] = cfg.max_upload_mb * 1024 * 1024
    app.config['UPLOAD_FOLDER'] = cfg.resolved_upload_root()
    app.config['SQLALCHEMY_DATABASE_URI'] = cfg.resolved_database_url(app.instance_path)
    app.config['RATELIMIT_ENABLED'] = cfg.rate_limit_enabled
    # Authoritative limiter store (memory:// by default; a shared store such as
    # redis:// makes the limits hold across gunicorn workers and restarts).
    app.config['RATELIMIT_STORAGE_URI'] = cfg.rate_limit_storage_uri
    app.config['TESTING'] = cfg.is_testing

    # 1.5 — session cookie hardening
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = cfg.is_production
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SECURE'] = cfg.is_production

    if cfg.flask_secret_key:
        app.secret_key = cfg.flask_secret_key
    else:
        app.secret_key = os.urandom(24)  # validate() already warned loudly

    # --- extensions -------------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)
    llm.init_service(cfg)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401

    with app.app_context():
        db.create_all()

    # --- blueprints ---------------------------------------------------------
    from .routes import analyze, auth, converse, datasets, misc
    app.register_blueprint(misc.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(datasets.bp)
    app.register_blueprint(analyze.bp)
    app.register_blueprint(converse.bp)

    # --- request lifecycle ----------------------------------------------------
    @app.before_request
    def assign_request_context():
        g.request_id = uuid.uuid4().hex[:8]
        if 'sid' not in session:
            session['sid'] = secrets.token_hex(16)        # 1.1 isolation key
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_hex(16)  # 1.5

    @app.before_request
    def csrf_protect():
        if not cfg.csrf_enabled or request.method not in ('POST', 'PUT', 'DELETE'):
            return None
        token = (request.headers.get('X-CSRF-Token')
                 or (request.form.get('csrf_token') if request.form else None))
        if token and token == session.get('csrf_token'):
            return None
        return jsonify({'error': 'Invalid or missing CSRF token. '
                                 'Reload the page and try again.'}), 403

    @app.before_request
    def require_auth():
        from .routes.auth import is_authorized
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        if not is_authorized():
            return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        return None

    @app.after_request
    def stamp_request_id(response):
        response.headers['X-Request-ID'] = g.get('request_id', '-')
        return response

    # --- error handling (1.6) ----------------------------------------------------
    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({'error':
                        f'File too large. The limit is {cfg.max_upload_mb} MB.'}), 413

    @app.errorhandler(429)
    def rate_limited(e):
        return jsonify({'error': 'Too many requests — please slow down. '
                                 f'({e.description})'}), 429

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({'error': 'Not found.'}), 404

    @app.errorhandler(Exception)
    def internal_error(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({'error': e.description}), e.code
        logger.exception("Unhandled server error")
        return jsonify({'error': 'Internal server error.'}), 500

    for warning in cfg.warnings:
        logger.warning("config: %s", warning)
    logger.info("STATlee ready (env=%s, sandbox=%s, storage=%s)",
                cfg.env, cfg.sandbox_mode, cfg.storage_backend)
    return app


app = create_app()
