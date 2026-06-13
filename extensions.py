"""Shared Flask extension instances.

Kept in their own module so blueprints can import them for decorators
without importing the app factory (avoids circular imports).
"""
from flask import session
from flask_limiter import Limiter
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy


def _session_key():
    """Rate-limit key: the per-browser session id set in app.before_request."""
    return session.get('sid', 'anonymous')


db = SQLAlchemy()
login_manager = LoginManager()
limiter = Limiter(key_func=_session_key, storage_uri='memory://')
