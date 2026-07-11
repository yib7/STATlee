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
import hmac
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

# P2-14 - defense-in-depth response headers. script-src is strict ('self' only,
# no 'unsafe-inline'): the two inline <script> blocks were moved into static JS
# files and every inline onclick= handler is now wired by a delegated listener
# in boot.js, so no executable inline script remains. The relaxations are only
# what the app actually needs:
#   style-src 'unsafe-inline'          - the templates use many inline style=""
#                                        attributes and <style> blocks (Tailwind
#                                        utility layout); style injection is far
#                                        lower risk than script injection.
#   style-src/font-src fonts.google*   - index.html + landing.html load the
#                                        Google Fonts stylesheet + font files.
#   img-src data:                      - matplotlib plots render as
#                                        <img src="data:image/png;base64,...">.
#   frame-src blob:                    - the Codebook tab previews an uploaded
#                                        PDF via a blob: object URL in an iframe.
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "frame-src 'self' blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)

# Alembic baseline revision (created by `flask db migrate` for the v1.2.0
# schema). A legacy create_all() database gets stamped at this revision so that
# future column-adding migrations apply on redeploy.
BASELINE_REVISION = '171777e71dff'

# DB URIs this process has already brought up to date, so re-building the app
# (a second gunicorn worker importing wsgi:app, or a test rebuild) does not
# re-run migrations against a DB it already handled.
_upgraded_uris = set()

# migrations/ lives at the repo root, a sibling of the statlee/ package, so the
# boot upgrade works regardless of the current working directory.
_MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'migrations'))


def _init_schema(app, cfg):
    """Bring the database schema up to date at boot.

    Testing keeps plain ``create_all()`` (fast, throwaway DBs with no
    alembic_version table). Every other env runs Alembic so that
    column-adding migrations actually apply on redeploy, guarded by
    ``_upgraded_uris`` so a given DB URI is only handled once per process:

    - no tables at all           -> fresh install, migrate to head;
    - tables but no alembic_version -> legacy create_all() DB, stamp the
      baseline then upgrade (existing rows are preserved);
    - alembic_version present     -> plain upgrade to head.
    """
    if cfg.is_testing:
        db.create_all()
        return

    uri = app.config['SQLALCHEMY_DATABASE_URI']
    if uri in _upgraded_uris:
        return

    from flask_migrate import stamp, upgrade
    from sqlalchemy import inspect as sa_inspect

    tables = set(sa_inspect(db.engine).get_table_names())
    # Tell migrations/env.py this is the in-process boot path so it leaves the
    # app's root-logger setup (RequestIdFilter, INFO level) alone; alembic.ini's
    # fileConfig would otherwise reconfigure root and silence app logging.
    os.environ['STATLEE_INPROCESS_MIGRATION'] = '1'
    try:
        if not tables:
            logger.info("DB schema: fresh database, migrating to head")
            upgrade()
        elif 'alembic_version' not in tables:
            logger.info("DB schema: legacy create_all() database, stamping "
                        "baseline %s then upgrading to head", BASELINE_REVISION)
            stamp(revision=BASELINE_REVISION)
            upgrade()
        else:
            logger.info("DB schema: already migrated, upgrading to head")
            upgrade()
    finally:
        os.environ.pop('STATLEE_INPROCESS_MIGRATION', None)

    _upgraded_uris.add(uri)
    # Drop every pooled connection opened during inspection/upgrade. With
    # gunicorn --preload, create_app runs once in the arbiter before fork; a
    # connection inherited across fork would be shared by multiple workers and
    # corrupt. Disposing here forces each worker to open its own. Harmless in a
    # single-process run (connections just reopen lazily).
    db.engine.dispose()


# Endpoints reachable without authorization (frontend loader, auth handshake).
PUBLIC_ENDPOINTS = {
    'misc.index', 'misc.welcome', 'static', 'misc.health_check',
    'auth.check_auth', 'auth.login', 'auth.register', 'auth.logout',
    'auth.verify_email', 'auth.request_password_reset', 'auth.reset_password',
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
    # Applies to every route with no explicit @limiter.limit(...); those
    # decorators still override this per-route.
    app.config['RATELIMIT_DEFAULT'] = cfg.rate_limit_default
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
    from flask_migrate import Migrate
    Migrate(app, db, directory=_MIGRATIONS_DIR)
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
        _init_schema(app, cfg)

    # --- blueprints ---------------------------------------------------------
    from .routes import analyze, auth, converse, datasets, misc
    app.register_blueprint(misc.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(datasets.bp)
    app.register_blueprint(analyze.bp)
    app.register_blueprint(converse.bp)

    # --- CLI commands -------------------------------------------------------
    from .cli import register_cli
    register_cli(app)

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
        # Constant-time compare (P2-8). The `token and` guard keeps a missing or
        # None token out of compare_digest; both sides are encoded because the
        # str overload of compare_digest raises on non-ASCII input, and a
        # garbage client token must be a normal 403 reject, not a 500.
        expected = session.get('csrf_token', '')
        if token and hmac.compare_digest(token.encode('utf-8', 'replace'),
                                         expected.encode('utf-8', 'replace')):
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

    # P1-6 — anonymous-data TTL cleanup previously only fired from the two
    # upload routes, so it never ran at all for a session that uploads once
    # and then just browses/analyzes. Run it opportunistically on any
    # request instead, throttled to once per 15 minutes so it stays cheap;
    # best-effort (a missing storage root or mid-flight FS error must never
    # break a real request).
    _cleanup_state = {'ts': 0.0}

    @app.before_request
    def ttl_cleanup():
        import time as _time
        now = _time.time()
        if now - _cleanup_state['ts'] > 900:
            _cleanup_state['ts'] = now
            try:
                from . import storage
                storage.cleanup_old_files(cfg.file_ttl_seconds)
            except Exception:
                logger.exception("TTL cleanup failed")

    @app.after_request
    def stamp_request_id(response):
        response.headers['X-Request-ID'] = g.get('request_id', '-')
        # P2-14 - security headers. setdefault so a route that sets its own
        # (e.g. a future embeddable widget) is not clobbered.
        response.headers.setdefault('Content-Security-Policy', CSP_POLICY)
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
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
