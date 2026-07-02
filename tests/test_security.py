"""Cross-cutting middleware: CSRF double-submit (1.5), per-session file
isolation over HTTP (1.1), generic errors (1.6), request-id stamping (3.3)."""
from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv


def test_index_serves_html(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'<!DOCTYPE html>' in resp.data or b'<!doctype html>' in resp.data


def test_welcome_landing_page_is_public(client):
    resp = client.get('/welcome')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'STATlee' in body
    assert 'plain English' in body


def test_health_and_metrics(client):
    assert client.get('/health').status_code == 200
    metrics = client.get('/metrics').get_json()
    assert 'uptime_seconds' in metrics
    assert 'llm_usage' in metrics


def test_post_without_csrf_is_rejected(client):
    # No X-CSRF-Token header -> blocked by csrf_protect before the handler.
    resp = client.post('/data_page', json={'filename': 'x.csv'})
    assert resp.status_code == 403
    assert 'CSRF' in resp.get_json()['error']


def test_post_with_csrf_passes_gate(client):
    # Valid token -> reaches handler (which then 400s on the missing dataset).
    resp = post_json(client, '/data_page', {'filename': 'nope.csv'})
    assert resp.status_code != 403


def test_unknown_route_is_generic_404(client):
    resp = client.get('/definitely-not-a-route')
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'Not found.'


def test_request_id_header_present(client):
    resp = client.get('/health')
    assert resp.headers.get('X-Request-ID')


def test_http_isolation_between_sessions(app):
    """Two independent clients upload the same filename; neither sees the
    other's bytes (anon_<sid> namespacing)."""
    client_a = app.test_client()
    client_b = app.test_client()

    upload_csv(client_a, SAMPLE_CSV, 'shared.csv')
    upload_csv(client_b, "age,income,group\n99,1,Z\n", 'shared.csv')

    page_a = post_json(client_a, '/data_page', {'filename': 'shared.csv'}).get_json()
    page_b = post_json(client_b, '/data_page', {'filename': 'shared.csv'}).get_json()

    assert page_a['total_rows'] == 4      # SAMPLE_CSV has 4 data rows
    assert page_b['total_rows'] == 1      # B uploaded a single row
    assert page_a['data'][0]['group'] == 'A'
    assert page_b['data'][0]['group'] == 'Z'


def test_csrf_token_is_stable_within_session(client):
    assert csrf_token(client) == csrf_token(client)


def test_rate_limit_key_is_ip_for_anonymous(app):
    """Anonymous traffic is keyed on client IP, not the resettable ``sid``
    cookie — so dropping cookies can't mint a fresh rate-limit bucket."""
    from statlee.extensions import _rate_limit_key
    with app.test_request_context('/chat', environ_base={'REMOTE_ADDR': '203.0.113.7'}):
        assert _rate_limit_key() == '203.0.113.7'


def test_rate_limit_key_is_account_for_logged_in(app):
    """A logged-in user is limited per account regardless of IP/cookies."""
    from unittest.mock import patch

    from statlee.extensions import _rate_limit_key

    class _FakeUser:
        is_authenticated = True
        id = 42

    with app.test_request_context('/chat'):
        with patch('flask_login.current_user', _FakeUser()):
            assert _rate_limit_key() == 'user_42'


def test_default_app_limiter_store_is_in_memory(app):
    # The default config keeps the in-memory store; production should override it.
    assert app.config['RATELIMIT_STORAGE_URI'] == 'memory://'


def test_app_propagates_configured_limiter_store(config):
    """A shared store from config reaches Flask-Limiter via app.config so the
    limits can be enforced across workers (no connection is opened here because
    rate limiting is disabled in the test config)."""
    from statlee.app import create_app
    config.rate_limit_storage_uri = 'redis://example:6379/2'
    application = create_app(config)
    assert application.config['RATELIMIT_STORAGE_URI'] == 'redis://example:6379/2'


def test_ratelimit_default_config_key_is_wired(config):
    """RATELIMIT_DEFAULT must reach Flask-Limiter's init_app config so routes
    with no explicit @limiter.limit(...) still get a default cap (P0-2)."""
    from statlee.app import create_app
    application = create_app(config)
    assert application.config['RATELIMIT_DEFAULT'] == config.rate_limit_default


def test_undecorated_route_429s_past_the_default_limit(config):
    """/check_auth carries no @limiter.limit decorator, so it only gets a cap
    via the RATELIMIT_DEFAULT config key. With a low default, the 3rd request
    within the window must be rejected with 429."""
    from statlee.app import create_app
    from statlee.extensions import limiter

    # The module-level Limiter singleton caches its default-limits group from
    # the *first* app that ever initialised it and never re-derives it (see
    # flask_limiter._extension.Limiter.init_app: it only sets default limits
    # `if not self.limit_manager._default_limits`). Every other test in this
    # suite runs with rate limiting disabled (init_app returns early), so this
    # is the first test to touch that state — reset it for test isolation
    # rather than depending on collection order.
    limiter.limit_manager._default_limits = []
    limiter.initialized = False

    config.rate_limit_enabled = True
    config.rate_limit_default = '2 per minute'
    application = create_app(config)
    test_client = application.test_client()

    try:
        first = test_client.get('/check_auth')
        second = test_client.get('/check_auth')
        third = test_client.get('/check_auth')

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
    finally:
        # Don't leak a 2-per-minute default into tests that run after this one.
        limiter.limit_manager._default_limits = []
        limiter.initialized = False
