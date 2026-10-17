"""Password-reset links (D5-7).

The reset link was built with url_for(..., _external=True), i.e. from the
request's Host header, so an attacker requesting a reset from
http://evil.example got the victim's emailed token pointed at their host. And
with no SMTP configured, the full link (token included) was logged at INFO in
every environment, so anyone with log access could take over any account.
"""
import logging

import pytest

from statlee.config import Config
from statlee.routes import auth as auth_mod

TOKEN = 'tok-SECRET-123'


class _FakeSMTP:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
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


def test_reset_link_comes_from_public_base_url_not_host_header(app, fake_smtp):
    cfg = _cfg(public_base_url='https://statlee.example', smtp_host='smtp.test')
    with app.test_request_context('/', base_url='http://evil.example'):
        auth_mod._send_password_reset_email(cfg, 'victim@example.com', TOKEN)
    assert len(fake_smtp.sent) == 1
    body = fake_smtp.sent[0]
    assert f'https://statlee.example/reset_password?token={TOKEN}' in body
    assert 'evil.example' not in body


def test_production_without_public_base_url_sends_no_host_derived_link(
        app, fake_smtp, caplog):
    cfg = _cfg(env='production', smtp_host='smtp.test')
    with caplog.at_level(logging.INFO, logger='statlee.auth'), \
            app.test_request_context('/', base_url='http://evil.example'):
        auth_mod._send_password_reset_email(cfg, 'victim@example.com', TOKEN)
    assert fake_smtp.sent == []
    assert any('PUBLIC_BASE_URL' in r.getMessage() for r in caplog.records)
    assert not any(TOKEN in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('env', ['production', 'testing'])
def test_reset_token_is_never_logged_outside_development(app, caplog, env):
    cfg = _cfg(env=env, public_base_url='https://statlee.example')
    with caplog.at_level(logging.DEBUG, logger='statlee.auth'), \
            app.test_request_context('/'):
        auth_mod._send_password_reset_email(cfg, 'victim@example.com', TOKEN)
    messages = [r.getMessage() for r in caplog.records]
    assert not any(TOKEN in m for m in messages)
    assert any('SMTP' in m for m in messages)


def test_development_still_logs_the_link_for_local_testing(app, caplog):
    cfg = _cfg(env='development')
    with caplog.at_level(logging.INFO, logger='statlee.auth'), \
            app.test_request_context('/'):
        auth_mod._send_password_reset_email(cfg, 'me@example.com', TOKEN)
    assert any(TOKEN in r.getMessage() for r in caplog.records)


def test_public_base_url_must_be_http_or_https():
    with pytest.raises(ValueError, match='PUBLIC_BASE_URL'):
        Config(env='testing', public_base_url='statlee.example').validate()


def test_public_base_url_from_env_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://statlee.example/')
    cfg = Config.from_env()
    assert cfg.external_url('/reset_password?token=x') == \
        'https://statlee.example/reset_password?token=x'


def test_production_accounts_without_smtp_or_base_url_warn():
    cfg = _cfg(env='production', accounts_enabled=True)
    assert any('SMTP_HOST' in w for w in cfg.warnings)
    assert any('PUBLIC_BASE_URL' in w for w in cfg.warnings)
    quiet = _cfg(env='production', accounts_enabled=True, smtp_host='smtp.test',
                 public_base_url='https://statlee.example')
    assert not any('SMTP_HOST' in w or 'PUBLIC_BASE_URL' in w for w in quiet.warnings)
