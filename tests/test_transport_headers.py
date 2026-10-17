"""Transport and cookie hardening read off real responses (Phase 4, 4.7)."""
import pytest
from conftest import FakeLLMService, csrf_token, post_json

from statlee import llm
from statlee.app import create_app
from statlee.config import Config


@pytest.fixture
def prod_app(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    cfg = Config(
        env='production', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'p.db').replace('\\', '/'),
        flask_secret_key='p' * 40, gemini_api_key='placeholder-not-a-key',
        rate_limit_enabled=False, accounts_enabled=True,
        public_base_url='https://statlee.example')
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(FakeLLMService())
    yield app
    from statlee.extensions import db
    with app.app_context():
        db.engine.dispose()


def _cookie_attrs(resp, name):
    for header in resp.headers.getlist('Set-Cookie'):
        if header.split('=', 1)[0] == name:
            return {part.strip().split('=', 1)[0].lower(): part.strip()
                    for part in header.split(';')[1:]}
    return None


def test_referrer_policy_on_every_response(client):
    for path in ('/', '/welcome', '/health', '/static/js/boot.js', '/nope'):
        assert client.get(path).headers.get('Referrer-Policy') == 'same-origin'


def test_no_cors_grant(client):
    resp = client.get('/', headers={'Origin': 'https://evil.example'})
    assert 'Access-Control-Allow-Origin' not in resp.headers
    assert 'Access-Control-Allow-Credentials' not in resp.headers


def test_hsts_only_in_production_over_https(client, prod_app):
    assert 'Strict-Transport-Security' not in client.get(
        '/', base_url='https://localhost').headers
    c = prod_app.test_client()
    assert 'Strict-Transport-Security' not in c.get(
        '/', base_url='http://localhost').headers
    hsts = c.get('/', base_url='https://localhost').headers.get(
        'Strict-Transport-Security')
    assert hsts and 'max-age=31536000' in hsts


def test_production_cookies_are_secure_httponly_and_samesite(prod_app):
    c = prod_app.test_client()
    base = 'https://localhost'
    session_cookie = _cookie_attrs(c.get('/', base_url=base), 'session')
    assert session_cookie is not None
    assert {'secure', 'httponly', 'samesite'} <= set(session_cookie)
    assert session_cookie['samesite'].lower() == 'samesite=lax'

    c.get('/', base_url=base)
    with c.session_transaction() as sess:
        token = sess['csrf_token']
    creds = {'email': 'p4@example.org', 'password': 'long-enough-password'}
    assert c.post('/register', json=creds, base_url=base,
                  headers={'X-CSRF-Token': token}).status_code == 201
    c.post('/logout', base_url=base, headers={'X-CSRF-Token': token})
    resp = c.post('/login', json=creds, base_url=base,
                  headers={'X-CSRF-Token': token})
    assert resp.status_code == 200
    remember = _cookie_attrs(resp, 'remember_token')
    assert remember is not None
    assert {'secure', 'httponly', 'samesite'} <= set(remember)
    assert remember['samesite'].lower() == 'samesite=lax'


def test_dev_remember_cookie_is_httponly_and_samesite(client):
    csrf_token(client)
    creds = {'email': 'dev@example.org', 'password': 'long-enough-password'}
    assert post_json(client, '/register', creds).status_code == 201
    post_json(client, '/logout', {})
    resp = post_json(client, '/login', creds)
    remember = _cookie_attrs(resp, 'remember_token')
    assert remember is not None
    assert 'httponly' in remember
    assert remember.get('samesite', '').lower() == 'samesite=lax'
