"""/health (D5-18): a cheap DB check, exempt from rate limits, and no
per-request side effects (no session minted, no TTL filesystem walk)."""
from statlee.config import Config


def _app(tmp_path, fake_llm, **kw):
    from statlee import llm
    from statlee.app import create_app
    cfg = Config(env='testing', flask_secret_key='s',
                 upload_root=str(tmp_path / 'uploads'),
                 database_url='sqlite:///' + str(tmp_path / 'h.db').replace('\\', '/'),
                 **kw)
    cfg.validate()
    application = create_app(cfg)
    llm.set_service(fake_llm)
    return application


def test_health_is_exempt_from_the_default_rate_limit(tmp_path, fake_llm,
                                                      reset_limiter_state):
    application = _app(tmp_path, fake_llm, rate_limit_enabled=True,
                       rate_limit_default='2 per minute')
    client = application.test_client()
    codes = [client.get('/health').status_code for _ in range(6)]
    assert codes == [200] * 6
    # The default limit still applies to ordinary routes.
    assert [client.get('/check_auth').status_code for _ in range(3)][-1] == 429


def test_health_mints_no_session(client):
    resp = client.get('/health')
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == 'OK'
    assert 'Set-Cookie' not in resp.headers


def test_health_reports_503_when_the_database_is_unreachable(app, client,
                                                             monkeypatch):
    from statlee.extensions import db

    def boom(*args, **kwargs):
        raise RuntimeError('database is gone')

    with app.app_context():
        monkeypatch.setattr(db.session, 'execute', boom)
        resp = client.get('/health')
    assert resp.status_code == 503
    assert 'database' in resp.get_data(as_text=True).lower()
