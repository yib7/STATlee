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
    assert 'Statly' in body
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
    from extensions import _rate_limit_key
    with app.test_request_context('/chat', environ_base={'REMOTE_ADDR': '203.0.113.7'}):
        assert _rate_limit_key() == '203.0.113.7'


def test_rate_limit_key_is_account_for_logged_in(app):
    """A logged-in user is limited per account regardless of IP/cookies."""
    from unittest.mock import patch

    from extensions import _rate_limit_key

    class _FakeUser:
        is_authenticated = True
        id = 42

    with app.test_request_context('/chat'):
        with patch('flask_login.current_user', _FakeUser()):
            assert _rate_limit_key() == 'user_42'
