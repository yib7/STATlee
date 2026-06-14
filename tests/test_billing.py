"""Monetization seam (workstream E).

The seam is intentionally a no-op today: ``check_and_debit`` always authorizes
and the new ``User`` columns just exist with safe defaults. These tests pin the
contract so wiring (the priority toggle) can depend on it, and so a future
real implementation has a regression net for the "always allowed today"
behaviour it will replace.
"""
import billing


def test_check_and_debit_allows_anonymous():
    allowed, message = billing.check_and_debit(None, priority=True)
    assert allowed is True
    assert message is None


def test_check_and_debit_allows_free_user(app):
    from extensions import db
    from models import User
    with app.app_context():
        user = User(email='seam@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        # Even a priority request on a zero-credit free account is allowed today.
        allowed, message = billing.check_and_debit(user, priority=True, cost=5)
        assert allowed is True
        assert message is None
        assert user.credits == 0      # no-op: nothing is debited yet


def test_new_user_defaults_to_free_plan_zero_credits(app):
    from extensions import db
    from models import User
    with app.app_context():
        user = User(email='defaults@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        assert user.plan == 'free'
        assert user.credits == 0


def test_check_auth_exposes_plan_and_credits(client, app):
    """A logged-in user's plan/credits surface in /check_auth for the UI."""
    from conftest import post_json
    post_json(client, '/register',
              {'email': 'me@example.com', 'password': 'password123'})
    payload = client.get('/check_auth').get_json()
    assert payload['user']['plan'] == 'free'
    assert payload['user']['credits'] == 0
