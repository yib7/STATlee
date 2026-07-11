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


def test_history_rows_beyond_cap_are_pruned(client, monkeypatch):
    """P2-13: saving past the per-user cap deletes the oldest rows in the same
    transaction, so a looping client cannot grow the table without bound."""
    import statlee.routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, 'HISTORY_MAX_ROWS', 5)
    post_json(client, '/register', {'email': 'cap@x.com', 'password': 'longenough1'})
    for i in range(8):
        saved = post_json(client, '/history', {'prompt': f'run {i}'})
        assert saved.get_json()['saved'] is True
    runs = client.get('/history').get_json()['runs']
    assert len(runs) == 5
    # The newest five survive; runs 0-2 were pruned.
    assert {r['prompt'] for r in runs} == {f'run {i}' for i in range(3, 8)}


def test_history_cap_is_per_user(client, monkeypatch):
    """P2-13: pruning one user's overflow must not touch another user's rows."""
    import statlee.routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, 'HISTORY_MAX_ROWS', 3)
    post_json(client, '/register', {'email': 'keep@x.com', 'password': 'longenough1'})
    post_json(client, '/history', {'prompt': 'keeper'})
    post_json(client, '/logout', {})
    post_json(client, '/register', {'email': 'flood@x.com', 'password': 'longenough1'})
    for i in range(5):
        post_json(client, '/history', {'prompt': f'flood {i}'})
    assert len(client.get('/history').get_json()['runs']) == 3
    post_json(client, '/logout', {})
    post_json(client, '/login', {'email': 'keep@x.com', 'password': 'longenough1'})
    kept = client.get('/history').get_json()['runs']
    assert [r['prompt'] for r in kept] == ['keeper']


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


def test_register_verification_email_send_failure_is_distinct(tmp_path, fake_llm, monkeypatch):
    """If sending the verification email raises, the user must NOT be told
    to 'check your email' — that inbox got nothing. The account is still
    created, but the response/status must be distinguishable."""
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()

    import statlee.routes.auth as auth_mod

    def boom(cfg, email, token):
        raise RuntimeError('smtp exploded')

    monkeypatch.setattr(auth_mod, '_send_verification_email', boom)
    resp = post_json(c, '/register', {'email': 'failmail@x.com', 'password': 'password123'})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['status'] == 'verification_email_failed'
    assert body['status'] != 'verification_required'
    # Account was still created.
    user = _user_by_email(app, 'failmail@x.com')
    assert user is not None
    assert user.email_verified is False


def test_register_verification_email_dev_no_smtp_is_not_a_failure(tmp_path, fake_llm):
    """No SMTP configured (dev mode) logs the link and returns normally —
    that is NOT a send failure."""
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()
    resp = post_json(c, '/register', {'email': 'devmode@x.com', 'password': 'password123'})
    assert resp.status_code == 202
    assert resp.get_json()['status'] == 'verification_required'


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


def test_verify_email_rejects_expired_token(tmp_path, fake_llm):
    """P2-12: a verification token older than the 48h window is rejected and
    does NOT log the user in, even though the token string still matches."""
    from datetime import UTC, datetime, timedelta

    from statlee.extensions import db
    from statlee.models import User
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()
    post_json(c, '/register', {'email': 'stale@x.com', 'password': 'password123'})
    token = _user_by_email(app, 'stale@x.com').verification_token

    with app.app_context():
        u = db.session.execute(
            db.select(User).filter_by(email='stale@x.com')).scalar_one()
        u.token_issued_at = datetime.now(UTC) - timedelta(hours=49)
        db.session.commit()

    resp = c.get(f'/verify_email?token={token}')
    assert resp.status_code == 400
    assert c.get('/check_auth').get_json().get('user') is None
    # Still unverified after the rejected attempt.
    assert _user_by_email(app, 'stale@x.com').email_verified is False


def test_verify_email_fresh_token_within_window_works(tmp_path, fake_llm):
    """P2-12 boundary: a token issued just inside the 48h window still confirms."""
    from datetime import UTC, datetime, timedelta

    from statlee.extensions import db
    from statlee.models import User
    app = _verify_app(tmp_path, fake_llm)
    c = app.test_client()
    post_json(c, '/register', {'email': 'fresh@x.com', 'password': 'password123'})
    token = _user_by_email(app, 'fresh@x.com').verification_token

    with app.app_context():
        u = db.session.execute(
            db.select(User).filter_by(email='fresh@x.com')).scalar_one()
        u.token_issued_at = datetime.now(UTC) - timedelta(hours=47)
        db.session.commit()

    resp = c.get(f'/verify_email?token={token}')
    assert resp.status_code == 302
    assert _user_by_email(app, 'fresh@x.com').email_verified is True


# ---------------------------------------------------------------------------
# Password reset (P2-11)
# ---------------------------------------------------------------------------

def _reset_token(app, email):
    from statlee.extensions import db
    from statlee.models import User
    with app.app_context():
        u = db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one()
        return u.password_reset_token


def test_password_reset_happy_path(client, app):
    """Request -> emailed token -> set new password -> log in with it, and the
    old password no longer works."""
    post_json(client, '/register',
              {'email': 'reset@x.com', 'password': 'origpass1'})
    post_json(client, '/logout', {})

    req = post_json(client, '/request_password_reset', {'email': 'reset@x.com'})
    assert req.status_code == 200
    token = _reset_token(app, 'reset@x.com')
    assert token

    # The GET form validates the token.
    assert client.get(f'/reset_password?token={token}').status_code == 200

    done = post_json(client, '/reset_password',
                     {'token': token, 'password': 'brandnew9'})
    assert done.status_code == 200

    # Old password rejected, new password accepted.
    post_json(client, '/logout', {})
    assert post_json(client, '/login',
                     {'email': 'reset@x.com', 'password': 'origpass1'}
                     ).status_code == 401
    assert post_json(client, '/login',
                     {'email': 'reset@x.com', 'password': 'brandnew9'}
                     ).status_code == 200
    # Token is single-use: it was cleared on success.
    assert _reset_token(app, 'reset@x.com') is None


def test_request_password_reset_unknown_email_is_200_no_enumeration(client):
    """An unregistered email gets the SAME generic 200 as a real one, so a
    caller cannot enumerate accounts."""
    resp = post_json(client, '/request_password_reset',
                     {'email': 'ghost@x.com'})
    assert resp.status_code == 200
    assert 'if that email' in resp.get_json()['message'].lower()


def test_reset_password_unknown_token_rejected(client):
    """A bogus token is rejected on both the GET (400 page) and the POST."""
    assert client.get('/reset_password?token=bogus').status_code == 400
    resp = post_json(client, '/reset_password',
                     {'token': 'bogus', 'password': 'brandnew9'})
    assert resp.status_code == 400
    assert 'invalid' in resp.get_json()['error'].lower()


def test_reset_password_expired_token_rejected(client, app):
    """A reset token older than the 1h window cannot set a new password."""
    from datetime import UTC, datetime, timedelta

    from statlee.extensions import db
    from statlee.models import User
    post_json(client, '/register',
              {'email': 'slowreset@x.com', 'password': 'origpass1'})
    post_json(client, '/request_password_reset', {'email': 'slowreset@x.com'})
    token = _reset_token(app, 'slowreset@x.com')

    with app.app_context():
        u = db.session.execute(
            db.select(User).filter_by(email='slowreset@x.com')).scalar_one()
        u.reset_token_issued_at = datetime.now(UTC) - timedelta(hours=2)
        db.session.commit()

    assert client.get(f'/reset_password?token={token}').status_code == 400
    resp = post_json(client, '/reset_password',
                     {'token': token, 'password': 'brandnew9'})
    assert resp.status_code == 400
    # The original password still works: nothing was changed.
    post_json(client, '/logout', {})
    assert post_json(client, '/login',
                     {'email': 'slowreset@x.com', 'password': 'origpass1'}
                     ).status_code == 200


def test_reset_password_rejects_short_password(client, app):
    post_json(client, '/register',
              {'email': 'shortpw@x.com', 'password': 'origpass1'})
    post_json(client, '/request_password_reset', {'email': 'shortpw@x.com'})
    token = _reset_token(app, 'shortpw@x.com')
    resp = post_json(client, '/reset_password', {'token': token, 'password': 'no'})
    assert resp.status_code == 400


def test_request_password_reset_is_rate_limited(tmp_path, fake_llm,
                                                reset_limiter_state):
    """P2-11: /request_password_reset shares the auth brute-force limit. With a
    2/min cap, the third request in a window gets a 429."""
    app = _rate_limited_auth_app(tmp_path, fake_llm)
    codes = [post_json(app.test_client(), '/request_password_reset',
                       {'email': f'u{i}@x.com'}).status_code for i in range(3)]
    assert codes[:2] == [200, 200]   # under the cap: generic always-200
    assert codes[2] == 429           # over the cap: rate limited


def test_verify_email_is_rate_limited(tmp_path, fake_llm, reset_limiter_state):
    """P2-11: /verify_email must be rate limited so the token endpoint can't be
    hammered. With a 2/min cap, the third request in a window gets a 429."""
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'a.db').replace('\\', '/'),
        flask_secret_key='k', rate_limit_enabled=True,
        rate_limit_verify='2 per minute',
        accounts_enabled=True, require_email_verification=True)
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    c = app.test_client()

    codes = [c.get('/verify_email?token=nope').status_code for _ in range(3)]
    assert codes[:2] == [400, 400]   # under the cap: normal (bad-token) response
    assert codes[2] == 429           # over the cap: rate limited


def _rate_limited_auth_app(tmp_path, fake_llm):
    """An app instance with a tight 2/min cap on /login and /register."""
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'a.db').replace('\\', '/'),
        flask_secret_key='k', rate_limit_enabled=True,
        rate_limit_auth='2 per minute', accounts_enabled=True)
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    return app


def test_login_is_rate_limited(tmp_path, fake_llm, reset_limiter_state):
    """P1-4: /login must carry a dedicated brute-force limit. With a 2/min cap,
    the third password guess in a window gets a 429."""
    c = _rate_limited_auth_app(tmp_path, fake_llm).test_client()
    codes = [post_json(c, '/login', {'email': 'a@b.com', 'password': 'nope'}
                       ).status_code for _ in range(3)]
    assert codes[:2] == [401, 401]   # under the cap: normal wrong-password response
    assert codes[2] == 429           # over the cap: rate limited


def test_register_is_rate_limited(tmp_path, fake_llm, reset_limiter_state):
    """P1-4: /register must carry the same cap so accounts can't be mass-created.
    Each attempt uses a fresh anonymous client (like a signup bot would), so
    every request lands in the same IP-keyed bucket."""
    app = _rate_limited_auth_app(tmp_path, fake_llm)
    codes = [post_json(app.test_client(), '/register',
                       {'email': f'u{i}@x.com', 'password': 'longenough1'}
                       ).status_code for i in range(3)]
    assert codes[:2] == [201, 201]   # under the cap: accounts created normally
    assert codes[2] == 429           # over the cap: rate limited


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


def _master_password_app(tmp_path, fake_llm):
    """An app instance gated by the legacy single APP_PASSWORD (no accounts)."""
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'a.db').replace('\\', '/'),
        flask_secret_key='k', rate_limit_enabled=False,
        app_password='correct-horse-battery-staple')
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    return app


def test_master_password_login_success(tmp_path, fake_llm):
    app = _master_password_app(tmp_path, fake_llm)
    c = app.test_client()
    resp = post_json(c, '/login', {'password': 'correct-horse-battery-staple'})
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'success'
    # The session is now authorized for protected routes (P2-8 regression
    # guard: secrets.compare_digest must still recognize the right password).
    assert c.get('/check_auth').get_json()['status'] == 'authorized'


def test_master_password_login_wrong_password(tmp_path, fake_llm):
    app = _master_password_app(tmp_path, fake_llm)
    c = app.test_client()
    resp = post_json(c, '/login', {'password': 'wrong'})
    assert resp.status_code == 401
    assert c.get('/check_auth').status_code == 401
