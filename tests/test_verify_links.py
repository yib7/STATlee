"""Email-verification links (D5-7 remainder, S5).

S3 moved password-reset links onto PUBLIC_BASE_URL and stopped logging their
tokens outside development. The verification link still came from the
request's Host header (an attacker registering from http://evil.example got a
victim-facing link pointing there) and, with no SMTP, was logged with its
token in every environment. It now follows the reset-link rules.
"""
import logging

import pytest

from statlee.config import Config
from statlee.routes import auth as auth_mod

TOKEN = 'verify-SECRET-456'


class _FakeSMTP:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, *args):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg.get_content())


@pytest.fixture
def fake_smtp(monkeypatch):
    import smtplib
    _FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, 'SMTP', _FakeSMTP)
    return _FakeSMTP


def _cfg(env='testing', **kw):
    cfg = Config(env=env, gemini_api_key='k', flask_secret_key='s', **kw)
    cfg.validate()
    return cfg


def test_verify_link_comes_from_public_base_url_not_host_header(app, fake_smtp):
    cfg = _cfg(public_base_url='https://statlee.example', smtp_host='smtp.test')
    with app.test_request_context('/', base_url='http://evil.example'):
        auth_mod._send_verification_email(cfg, 'victim@example.com', TOKEN)
    assert len(fake_smtp.sent) == 1
    body = fake_smtp.sent[0]
    assert f'https://statlee.example/verify_email?token={TOKEN}' in body
    assert 'evil.example' not in body


def test_production_without_public_base_url_sends_nothing_and_says_so(
        app, fake_smtp, caplog):
    cfg = _cfg(env='production', smtp_host='smtp.test')
    with caplog.at_level(logging.INFO, logger='statlee.auth'), \
            app.test_request_context('/', base_url='http://evil.example'), \
            pytest.raises(RuntimeError, match='PUBLIC_BASE_URL'):
        auth_mod._send_verification_email(cfg, 'victim@example.com', TOKEN)
    assert fake_smtp.sent == []
    assert any('PUBLIC_BASE_URL' in r.getMessage() for r in caplog.records)
    assert not any(TOKEN in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('env', ['production', 'testing'])
def test_verify_token_is_never_logged_outside_development(app, caplog, env):
    cfg = _cfg(env=env, public_base_url='https://statlee.example')
    with caplog.at_level(logging.DEBUG, logger='statlee.auth'), \
            app.test_request_context('/'):
        try:
            auth_mod._send_verification_email(cfg, 'victim@example.com', TOKEN)
        except RuntimeError:
            pass   # production with no SMTP reports the failure to /register
    messages = [r.getMessage() for r in caplog.records]
    assert not any(TOKEN in m for m in messages)
    assert any('SMTP' in m for m in messages)


def test_development_still_logs_the_verify_link(app, caplog):
    cfg = _cfg(env='development')
    with caplog.at_level(logging.INFO, logger='statlee.auth'), \
            app.test_request_context('/'):
        auth_mod._send_verification_email(cfg, 'me@example.com', TOKEN)
    assert any(TOKEN in r.getMessage() for r in caplog.records)


def test_register_in_production_without_a_link_origin_says_the_email_failed(
        app, client, monkeypatch):
    cfg = app.config['STATLEE']
    monkeypatch.setattr(cfg, 'env', 'production')
    monkeypatch.setattr(cfg, 'require_email_verification', True)
    monkeypatch.setattr(cfg, 'smtp_host', 'smtp.test')
    monkeypatch.setattr(cfg, 'public_base_url', '')
    monkeypatch.setattr(cfg, 'csrf_enabled', False)
    resp = client.post('/register', json={'email': 'p@example.com',
                                          'password': 'password123'})
    assert resp.status_code == 202
    assert resp.get_json()['status'] == 'verification_email_failed'
