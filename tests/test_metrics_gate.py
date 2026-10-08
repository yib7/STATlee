"""/metrics is an operator endpoint (D5-18 remainder, S5).

It returns process uptime and the per-model LLM call, token and cost totals
for every user of the server. In the default open mode any anonymous visitor
could read it. Now:

* METRICS_TOKEN set: only a request carrying it (``Authorization: Bearer``
  or ``X-Metrics-Token``) gets the snapshot, in every environment, without a
  session or login (a scraper has neither);
* METRICS_TOKEN unset: 404 in production; development and testing keep the
  old behaviour (behind the normal app gate).
"""
from statlee.config import Config

TOKEN = 'metrics-test-token'


def _set(app, monkeypatch, **values):
    cfg = app.config['STATLEE']
    for key, value in values.items():
        monkeypatch.setattr(cfg, key, value)


def test_production_without_a_token_hides_metrics(app, client, monkeypatch):
    _set(app, monkeypatch, env='production', metrics_token='')
    assert client.get('/metrics').status_code == 404


def test_a_configured_token_is_required(app, client, monkeypatch):
    _set(app, monkeypatch, env='production', metrics_token=TOKEN)
    assert client.get('/metrics').status_code == 401
    assert client.get('/metrics', headers={
        'Authorization': 'Bearer wrong'}).status_code == 401
    ok = client.get('/metrics', headers={'Authorization': f'Bearer {TOKEN}'})
    assert ok.status_code == 200
    assert 'llm_usage' in ok.get_json()
    alt = client.get('/metrics', headers={'X-Metrics-Token': TOKEN})
    assert alt.status_code == 200


def test_token_works_without_a_login_when_login_is_required(
        app, client, monkeypatch):
    _set(app, monkeypatch, env='production', metrics_token=TOKEN,
         require_login=True)
    assert client.get('/metrics').status_code == 401
    resp = client.get('/metrics', headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    assert 'Set-Cookie' not in resp.headers          # a scrape mints no session


def test_token_is_required_in_development_too_once_set(app, client, monkeypatch):
    _set(app, monkeypatch, env='development', metrics_token=TOKEN)
    assert client.get('/metrics').status_code == 401


def test_testing_without_a_token_stays_open(client):
    assert client.get('/metrics').status_code == 200


def test_metrics_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv('METRICS_TOKEN', '  ' + TOKEN + '  ')
    assert Config.from_env().metrics_token == TOKEN

