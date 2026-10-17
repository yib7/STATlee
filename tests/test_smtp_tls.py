"""Every outgoing email verifies the SMTP server's certificate (Phase 4, 4.1).

``smtplib.SMTP.starttls()`` with no context uses an UNVERIFIED TLS context, so
an attacker on the path to SMTP_HOST could pose as the server and collect
SMTP_PASSWORD plus the reset and verification links. All three senders
(verification, password reset, issue report) must pass a verifying context.
"""
import ssl
from types import SimpleNamespace

import pytest

from statlee.config import Config
from statlee.routes import auth as auth_mod
from statlee.routes import misc as misc_mod


class _RecordingSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.contexts = []
        self.logged_in_after_tls = None
        self.sent = []
        _RecordingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.contexts.append(context)

    def login(self, user, password):
        self.logged_in_after_tls = bool(self.contexts)

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    import smtplib
    _RecordingSMTP.instances = []
    monkeypatch.setattr(smtplib, 'SMTP', _RecordingSMTP)
    return _RecordingSMTP


def _cfg(**kw):
    cfg = Config(env='testing', gemini_api_key='k', flask_secret_key='s',
                 smtp_host='smtp.test', smtp_user='mailer@example.org',
                 smtp_password='not-a-real-password',
                 public_base_url='https://statlee.example', **kw)
    cfg.validate()
    return cfg


def _assert_verified(smtp):
    assert len(smtp.instances) == 1
    conn = smtp.instances[0]
    assert len(conn.contexts) == 1
    ctx = conn.contexts[0]
    assert isinstance(ctx, ssl.SSLContext), 'starttls() got no TLS context'
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert conn.logged_in_after_tls is True
    assert len(conn.sent) == 1


def test_verification_email_verifies_the_server(app, smtp):
    with app.test_request_context('/'):
        auth_mod._send_verification_email(_cfg(), 'new@example.org', 'tok')
    _assert_verified(smtp)


def test_password_reset_email_verifies_the_server(app, smtp):
    with app.test_request_context('/'):
        auth_mod._send_password_reset_email(_cfg(), 'user@example.org', 'tok')
    _assert_verified(smtp)


def test_issue_report_email_verifies_the_server(app, smtp):
    report = SimpleNamespace(id=7, description='d', code='', output='',
                             console_errors='', user_agent='ua')
    cfg = _cfg(issue_report_to='ops@example.org')
    misc_mod._email_report(cfg, report)
    _assert_verified(smtp)
