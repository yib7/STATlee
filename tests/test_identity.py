"""``current_user_or_none()`` — the single "logged-in User, or None" helper
(P2-5 consolidation of the ``current_user and getattr(..., 'is_authenticated',
False)`` idiom that used to be re-implemented in several modules)."""
from unittest.mock import patch

from statlee.identity import current_user_or_none


def test_current_user_or_none_is_none_when_anonymous(app):
    with app.test_request_context('/'):
        assert current_user_or_none() is None


def test_current_user_or_none_returns_the_user_when_authenticated(app):
    class _FakeUser:
        is_authenticated = True
        id = 42

    with app.test_request_context('/'):
        with patch('flask_login.current_user', _FakeUser()):
            u = current_user_or_none()
            assert u is not None
            assert u.id == 42


def test_current_user_or_none_is_none_when_not_authenticated():
    """Explicitly is_authenticated=False (as opposed to no attribute at all)."""
    class _FakeUser:
        is_authenticated = False

    with patch('flask_login.current_user', _FakeUser()):
        assert current_user_or_none() is None


def test_current_user_or_none_is_safe_without_app_context():
    # No Flask app/request context at all, and no login manager initialised —
    # must not raise (mirrors storage.current_identity's try/except safety).
    assert current_user_or_none() is None
