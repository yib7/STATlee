"""Anonymous callers and metered requests (release audit 2026-10-16, B-05).

With BILLING_ENABLED on, check_and_debit only debited a logged-in free-plan
user, so an anonymous caller (or a user who ran out of credits and signed out)
got Pro mode and the report builder for free. A metered request now needs an
account; the refusal happens before any model call and never consumes a unit
of the operator's monthly priority ceiling.
"""
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import billing
from statlee.config import Config


def test_anonymous_priority_is_refused_with_billing_on():
    cfg = Config(env='testing', billing_enabled=True,
                 monthly_priority_call_ceiling=5)
    billing.reset_spend()
    try:
        allowed, message = billing.check_and_debit(None, priority=True, config=cfg)
        assert allowed is False
        assert message == billing.ANONYMOUS_METERED_MESSAGE
        assert 'sign in' in message.lower()
        assert billing._spend['priority_calls'] == 0   # no ceiling unit taken
    finally:
        billing.reset_spend()


def test_anonymous_unmetered_request_still_passes_with_billing_on():
    cfg = Config(env='testing', billing_enabled=True)
    assert billing.check_and_debit(None, priority=False, config=cfg) == (True, None)


def test_anonymous_priority_still_allowed_with_billing_off():
    cfg = Config(env='testing', billing_enabled=False)
    assert billing.check_and_debit(None, priority=True, config=cfg) == (True, None)


def test_anonymous_pro_chat_is_402_before_any_draft(app, client, fake_llm):
    app.config['STATLEE'].billing_enabled = True
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'regress income on age',
                                       'pro': True})
    assert resp.status_code == 402
    assert 'sign in' in resp.get_json()['error'].lower()
    assert not [c for c in fake_llm.calls if c[1] in ('draft', 'validation')]


def test_anonymous_default_chat_still_works_with_billing_on(app, client, fake_llm):
    app.config['STATLEE'].billing_enabled = True
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'summarize'})
    assert resp.status_code == 200
    assert any(e.get('type') == 'done' for e in sse_events(resp))


def test_anonymous_report_is_402_before_any_draft(app, client, fake_llm):
    app.config['STATLEE'].billing_enabled = True
    resp = post_json(client, '/generate_report',
                     {'output': 'coef = 1.2, p = 0.01',
                      'interpretation': 'A strong effect.'})
    assert resp.status_code == 402
    assert not [c for c in fake_llm.calls if c[1] in ('report_draft', 'report')]


def test_config_warns_when_billing_is_on_without_required_login():
    cfg = Config(env='development', billing_enabled=True, require_login=False)
    cfg.validate()
    assert any('REQUIRE_LOGIN' in w for w in cfg.warnings)
    quiet = Config(env='development', billing_enabled=True, require_login=True)
    quiet.validate()
    assert not any('BILLING_ENABLED is on without' in w for w in quiet.warnings)
