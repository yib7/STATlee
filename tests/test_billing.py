"""Monetization seam (workstream E).

The seam is intentionally a no-op today: ``check_and_debit`` always authorizes
and the new ``User`` columns just exist with safe defaults. These tests pin the
contract so wiring (Pro mode) can depend on it, and so a future
real implementation has a regression net for the "always allowed today"
behaviour it will replace.
"""
from statlee import billing


def test_check_and_debit_allows_anonymous():
    allowed, message = billing.check_and_debit(None, priority=True)
    assert allowed is True
    assert message is None


def test_check_and_debit_allows_free_user(app):
    from statlee.extensions import db
    from statlee.models import User
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
    from statlee.extensions import db
    from statlee.models import User
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


# --- Billing turned on (workstream E, behind BILLING_ENABLED) ----------------

def test_billing_disabled_config_is_still_a_noop():
    from statlee.config import Config
    cfg = Config(env='testing', billing_enabled=False)
    allowed, message = billing.check_and_debit(None, priority=True, config=cfg)
    assert allowed is True
    assert message is None


def test_billing_enabled_denies_free_user_without_credits(app):
    from statlee.config import Config
    from statlee.extensions import db
    from statlee.models import User
    cfg = Config(env='testing', billing_enabled=True)
    with app.app_context():
        user = User(email='broke@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        allowed, message = billing.check_and_debit(
            user, priority=True, config=cfg)
        assert allowed is False
        assert 'credit' in message.lower()
        assert user.credits == 0


def test_billing_enabled_debits_a_credit(app):
    from statlee.config import Config
    from statlee.extensions import db
    from statlee.models import User
    cfg = Config(env='testing', billing_enabled=True)
    with app.app_context():
        user = User(email='rich@example.com')
        user.set_password('password123')
        user.credits = 3
        db.session.add(user)
        db.session.commit()
        allowed, message = billing.check_and_debit(
            user, priority=True, config=cfg)
        assert allowed is True and message is None
        assert user.credits == 2


def test_monthly_priority_ceiling_blocks_when_exceeded():
    from statlee.config import Config
    cfg = Config(env='testing', billing_enabled=True,
                 monthly_priority_call_ceiling=1)
    billing.reset_spend()
    try:
        allowed_first, _ = billing.check_and_debit(None, priority=True, config=cfg)
        allowed_second, message = billing.check_and_debit(
            None, priority=True, config=cfg)
    finally:
        billing.reset_spend()
    assert allowed_first is True
    assert allowed_second is False
    assert 'ceiling' in message.lower()


def test_monthly_ceiling_ignores_non_priority():
    from statlee.config import Config
    cfg = Config(env='testing', billing_enabled=True,
                 monthly_priority_call_ceiling=1)
    billing.reset_spend()
    try:
        # Non-priority requests never consume the ceiling.
        for _ in range(5):
            allowed, _msg = billing.check_and_debit(None, priority=False, config=cfg)
            assert allowed is True
    finally:
        billing.reset_spend()
