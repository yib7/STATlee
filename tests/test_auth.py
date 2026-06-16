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
