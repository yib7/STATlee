"""Gate coverage over the whole URL map (Phase 4, 4.5 and 4.6).

Walks ``app.url_map`` instead of a hand-written list, so a route added later
is covered without anyone remembering to add it here:

- with ``REQUIRE_LOGIN`` on (and with the legacy master password), every
  endpoint outside the public allowlist answers 401 to a caller with no
  session, before its handler runs (no model call is made);
- every billed or credential endpoint has its own limit tighter than the
  default, keyed per client IP, so dropping the cookie does not reset it.
"""
import re

import pytest
from conftest import FakeLLMService

from statlee import llm
from statlee.app import PUBLIC_ENDPOINTS, create_app
from statlee.config import Config

# The endpoints reachable without signing in. Growing this set is a security
# decision, so it is pinned here as well as in app.py.
EXPECTED_PUBLIC = {
    'misc.index', 'misc.welcome', 'static', 'misc.health_check', 'misc.metrics',
    'auth.check_auth', 'auth.login', 'auth.register', 'auth.logout',
    'auth.verify_email', 'auth.request_password_reset', 'auth.reset_password',
}

# Endpoints that can reach a paid model call or the sandbox, plus the
# credential endpoints a guesser would hammer.
LIMITED = {
    'analyze.chat', 'analyze.run_code', 'analyze.interpret_results',
    'analyze.method_prompt', 'converse.converse', 'misc.generate_report',
    'datasets.suggest_analysis', 'datasets.wrangle',
    'datasets.classify_variables', 'datasets.extract_pdf_codebook',
    'datasets.upload_pdf', 'datasets.upload_file', 'export.export_project',
    'misc.report_issue', 'auth.login', 'auth.register',
    'auth.request_password_reset', 'auth.reset_password', 'auth.verify_email',
}


def _app(tmp_path, **kw):
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'g.db').replace('\\', '/'),
        flask_secret_key='k', **kw)
    cfg.validate()
    app = create_app(cfg)
    fake = FakeLLMService()
    llm.set_service(fake)
    return app, fake


def _url(rule):
    return re.sub(r'<(?:[^:>]+:)?([^>]+)>', 'r0001', rule.rule)


def _protected_requests(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
            yield rule.endpoint, method, _url(rule)


def test_public_allowlist_is_exactly_the_reviewed_set():
    assert PUBLIC_ENDPOINTS == EXPECTED_PUBLIC


@pytest.mark.parametrize('mode', ['require_login', 'master_password'])
def test_every_protected_endpoint_rejects_a_caller_without_a_session(
        tmp_path, mode):
    kw = ({'require_login': True} if mode == 'require_login'
          else {'app_password': 'gate-test-password'})
    app, fake = _app(tmp_path, rate_limit_enabled=False, **kw)
    client = app.test_client()
    client.get('/')
    with client.session_transaction() as sess:
        token = sess['csrf_token']
    seen = []
    for endpoint, method, url in _protected_requests(app):
        resp = client.open(url, method=method, json={},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 401, (endpoint, method, resp.status_code)
        seen.append(endpoint)
    assert len(set(seen)) >= 25          # the walk really covered the app
    assert fake.calls == []


def test_every_limited_endpoint_has_its_own_limit(tmp_path,
                                                  reset_limiter_state):
    app, fake = _app(
        tmp_path, rate_limit_enabled=True, rate_limit_default='1000 per minute',
        rate_limit_run='1 per minute', rate_limit_chat='1 per minute',
        rate_limit_auth='1 per minute', rate_limit_verify='1 per minute',
        rate_limit_data_page='1000 per minute')
    by_endpoint = {rule.endpoint: rule for rule in app.url_map.iter_rules()}
    assert LIMITED <= set(by_endpoint)
    for endpoint in sorted(LIMITED):
        rule = by_endpoint[endpoint]
        method = 'POST' if 'POST' in rule.methods else 'GET'
        codes = []
        limit = 5 if endpoint == 'misc.report_issue' else 1
        for _ in range(limit + 1):
            # A fresh cookie jar each time: the limit must not reset.
            fresh = app.test_client()
            fresh.get('/')
            with fresh.session_transaction() as sess:
                tok = sess['csrf_token']
            resp = fresh.open(_url(rule), method=method, json={},
                              headers={'X-CSRF-Token': tok})
            codes.append(resp.status_code)
        assert codes[-1] == 429, (endpoint, codes)
        assert 429 not in codes[:-1], (endpoint, codes)
    assert fake.calls == []
