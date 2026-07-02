"""Accounts (7.1), the open/password/accounts gate, and per-user history
persistence (5.7/7.2)."""
import pytest
from conftest import post_json


def test_check_auth_open_mode(client):
    body = client.get('/check_auth').get_json()
    assert body['status'] == 'authorized'
    assert body['mode'] == 'open'
    assert body['user'] is None


def test_register_then_logged_in(client):
    resp = post_json(client, '/register',
                     {'email': 'a@b.com', 'password': 'longenough1'})
    assert resp.status_code == 201
    assert resp.get_json()['user']['email'] == 'a@b.com'
    # Session now reports the logged-in user.
    auth = client.get('/check_auth').get_json()
    assert auth['user']['email'] == 'a@b.com'


def test_register_rejects_bad_email(client):
    resp = post_json(client, '/register',
                     {'email': 'not-an-email', 'password': 'longenough1'})
    assert resp.status_code == 400


def test_register_rejects_short_password(client):
    resp = post_json(client, '/register',
                     {'email': 'x@y.com', 'password': 'short'})
    assert resp.status_code == 400


def test_register_duplicate_conflict(client):
    post_json(client, '/register', {'email': 'dup@x.com', 'password': 'longenough1'})
    resp = post_json(client, '/register',
                     {'email': 'dup@x.com', 'password': 'longenough1'})
    assert resp.status_code == 409


def test_register_race_duplicate_commit_returns_409(client, monkeypatch):
    """Two concurrent registrations for the same email both pass the SELECT
    pre-check; the second commit hits the unique constraint. That must yield
    a clean 409, not a generic 500, and the session must be rolled back."""
    from statlee.extensions import db

    real_commit = db.session.commit
    calls = {'n': 0}

    def flaky_commit():
        calls['n'] += 1
        if calls['n'] == 1:
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError('INSERT', {}, Exception('UNIQUE constraint failed'))
        return real_commit()

    monkeypatch.setattr(db.session, 'commit', flaky_commit)
    resp = post_json(client, '/register',
                     {'email': 'race@x.com', 'password': 'longenough1'})
    assert resp.status_code == 409
    assert 'already exists' in resp.get_json()['error'].lower()
    # Session was rolled back and is usable again for a fresh registration.
    resp2 = post_json(client, '/register',
                      {'email': 'race2@x.com', 'password': 'longenough1'})
    assert resp2.status_code == 201


def test_login_wrong_password(client):
    post_json(client, '/register', {'email': 'c@d.com', 'password': 'longenough1'})
    post_json(client, '/logout', {})
    resp = post_json(client, '/login',
                     {'email': 'c@d.com', 'password': 'wrongpassword'})
    assert resp.status_code == 401


def test_login_logout_cycle(client):
    post_json(client, '/register', {'email': 'e@f.com', 'password': 'longenough1'})
    assert post_json(client, '/logout', {}).status_code == 200
    assert client.get('/check_auth').get_json()['user'] is None
    ok = post_json(client, '/login', {'email': 'e@f.com', 'password': 'longenough1'})
    assert ok.status_code == 200


def test_history_anonymous_is_not_persisted(client):
    # Anonymous: GET returns empty/not-persisted, POST refuses to store.
    assert client.get('/history').get_json()['persisted'] is False
    saved = post_json(client, '/history',
                      {'prompt': 'p', 'code': 'c', 'dataset_name': 'd.csv'})
    assert saved.get_json()['saved'] is False


def test_history_persists_for_logged_in_user(client):
    post_json(client, '/register', {'email': 'g@h.com', 'password': 'longenough1'})
    saved = post_json(client, '/history',
                      {'prompt': 'run OLS', 'code': "print('x')",
                       'language': 'Python', 'dataset_name': 'study.csv',
                       'output': 'coef=1', 'interpretation': 'sig'})
    assert saved.get_json()['saved'] is True
    runs = client.get('/history').get_json()
    assert runs['persisted'] is True
    assert runs['runs'][0]['prompt'] == 'run OLS'


def test_history_save_truncates_oversized_fields(client):
    post_json(client, '/register', {'email': 'trunc@x.com', 'password': 'longenough1'})
    saved = post_json(client, '/history', {
        'prompt': 'p' * 10500,
        'code': 'c' * 20500,
        'output': 'o' * 20500,
        'interpretation': 'i' * 20500,
        'dataset_name': 'd' * 300,
        'language': 'l' * 30,
    })
    assert saved.get_json()['saved'] is True
    run = client.get('/history').get_json()['runs'][0]
    assert len(run['prompt']) == 10000
    assert len(run['code']) == 20000
    assert len(run['output']) == 20000
    assert len(run['interpretation']) == 20000
    assert len(run['dataset_name']) == 255
    assert len(run['language']) == 16


def _verify_app(tmp_path, fake_llm):
    """An app instance with email verification required."""
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'a.db').replace('\\', '/'),
        flask_secret_key='k', rate_limit_enabled=False,
        accounts_enabled=True, require_email_verification=True)
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    return app


def _user_by_email(app, email):
    from statlee.extensions import db
    from statlee.models import User
    with app.app_context():
        return db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one()


def test_register_requires_verification_when_enabled(tmp_path, fake_llm):
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()
    resp = post_json(c, '/register', {'email': 'v@x.com', 'password': 'password123'})
    assert resp.status_code == 202
    assert resp.get_json()['status'] == 'verification_required'
    # Not logged in until verified.
    assert c.get('/check_auth').get_json().get('user') is None
    user = _user_by_email(app, 'v@x.com')
    assert user.email_verified is False
    assert user.verification_token


def test_login_blocked_until_verified(tmp_path, fake_llm):
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()
    post_json(c, '/register', {'email': 'v2@x.com', 'password': 'password123'})
    resp = post_json(c, '/login', {'email': 'v2@x.com', 'password': 'password123'})
    assert resp.status_code == 403
    assert 'confirm your email' in resp.get_json()['error'].lower()


def test_verify_email_confirms_and_logs_in(tmp_path, fake_llm):
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()
    post_json(c, '/register', {'email': 'v3@x.com', 'password': 'password123'})
    token = _user_by_email(app, 'v3@x.com').verification_token

    resp = c.get(f'/verify_email?token={token}')
    assert resp.status_code == 302                       # redirect to '/'
    assert c.get('/check_auth').get_json()['user']['email'] == 'v3@x.com'
    user = _user_by_email(app, 'v3@x.com')
    assert user.email_verified is True
    assert user.verification_token is None


def test_verify_email_rejects_bad_token(tmp_path, fake_llm):
    app = _verify_app(tmp_path, fake_llm)
    assert app.test_client().get('/verify_email?token=nope').status_code == 400


@pytest.mark.parametrize('require_login', [True])
def test_require_login_blocks_protected_routes(tmp_path, fake_llm, require_login):
    """REQUIRE_LOGIN=true makes the sandbox closed: protected routes 401 until
    the user authenticates (7.4)."""
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'a.db').replace('\\', '/'),
        flask_secret_key='k', rate_limit_enabled=False,
        require_login=require_login)
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    c = app.test_client()

    # check_auth is public but reports unauthorized.
    assert c.get('/check_auth').status_code == 401
    # A protected route is blocked before reaching its handler.
    resp = post_json(c, '/data_page', {'filename': 'x.csv'})
    assert resp.status_code == 401
