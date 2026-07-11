"""Operator CLI commands (P2-10).

``flask grant-credits <email> <n>`` is the minimum-viable manual grant path for
a billing deployment where a free account otherwise starts (and stays) at 0
credits. Invoked here through Flask's test CLI runner so no shell is needed.
"""
from statlee.extensions import db
from statlee.models import User


def _make_user(app, email, credits=0):
    with app.app_context():
        u = User(email=email)
        u.set_password('password123')
        u.credits = credits
        db.session.add(u)
        db.session.commit()
        return u.id


def test_grant_credits_adds_to_balance(app):
    uid = _make_user(app, 'grantme@example.com', credits=2)
    result = app.test_cli_runner().invoke(
        args=['grant-credits', 'grantme@example.com', '7'])
    assert result.exit_code == 0
    assert '9' in result.output          # 2 + 7 = 9, reported in the message
    with app.app_context():
        assert db.session.get(User, uid).credits == 9


def test_grant_credits_is_case_insensitive_on_email(app):
    uid = _make_user(app, 'mixed@example.com', credits=0)
    result = app.test_cli_runner().invoke(
        args=['grant-credits', 'MIXED@Example.com', '3'])
    assert result.exit_code == 0
    with app.app_context():
        assert db.session.get(User, uid).credits == 3


def test_grant_credits_unknown_email_errors_without_change(app):
    result = app.test_cli_runner().invoke(
        args=['grant-credits', 'ghost@example.com', '5'])
    assert result.exit_code != 0
    assert 'no account' in result.output.lower()
