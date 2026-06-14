"""Shared Flask extension instances.

Kept in their own module so blueprints can import them for decorators
without importing the app factory (avoids circular imports).
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy


def _rate_limit_key():
    """Per-identity rate-limit key.

    Logged-in users are limited per account; anonymous callers per client IP.

    Deliberately NOT keyed on the server-set ``sid`` cookie: that cookie is
    minted by the server, so an abuser who simply discards cookies between
    requests would get a fresh, empty rate-limit bucket every time and bypass
    the limits entirely — running up the Gemini bill. Keying anonymous traffic
    on the source IP closes that bypass. (Behind a proxy the real client IP
    requires ProxyFix; see ``TRUST_PROXY_HOPS`` in app.py / config.py.)
    """
    try:
        from flask_login import current_user
        if current_user and getattr(current_user, 'is_authenticated', False):
            return f"user_{current_user.id}"
    except Exception:  # login manager not ready (scripts/tests)
        pass
    return get_remote_address()


db = SQLAlchemy()
login_manager = LoginManager()
limiter = Limiter(key_func=_rate_limit_key, storage_uri='memory://')
