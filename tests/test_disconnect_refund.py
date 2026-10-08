"""A client that disconnects mid-stream gets its debited credit back (deep
audit D1-15). The WSGI server closes the generator, which raises
GeneratorExit (a BaseException, so ``except Exception`` refund paths never ran);
a generator closed before its first step runs none of its body at all."""
from collections import Counter

import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import billing


@pytest.fixture
def ledger(monkeypatch):
    calls = Counter()

    def debit(*_a, **_k):
        calls['debit'] += 1
        return True, None

    def refund(*_a, **_k):
        calls['refund'] += 1

    monkeypatch.setattr(billing, 'check_and_debit', debit)
    monkeypatch.setattr(billing, 'refund', refund)
    return calls


def _chat(client):
    upload_csv(client, SAMPLE_CSV)
    return post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'summarize', 'pro': True})


def test_chat_disconnect_mid_stream_refunds(client, ledger):
    resp = _chat(client)
    it = iter(resp.response)
    next(it)                 # first SSE event reached the client...
    resp.close()             # ...then the client went away
    assert ledger == Counter(debit=1, refund=1)


def test_chat_disconnect_before_first_byte_refunds(client, ledger):
    resp = _chat(client)
    resp.close()
    assert ledger == Counter(debit=1, refund=1)


def test_chat_completed_stream_is_not_refunded(client, ledger):
    events = sse_events(_chat(client))
    assert any(e.get('type') == 'done' for e in events)
    assert ledger == Counter(debit=1)


def test_chat_guard_block_refunds_exactly_once(client, ledger, fake_llm):
    fake_llm.block_code('shell')
    events = sse_events(_chat(client))
    assert any(e.get('type') == 'error' for e in events)
    assert ledger == Counter(debit=1, refund=1)


@pytest.mark.parametrize('read_first_event', [True, False])
def test_real_credit_comes_back_after_disconnect(client, app, read_first_event):
    """With real billing and a logged-in free user: debit on /chat, disconnect,
    and the balance is restored (the before-first-byte case runs in the
    response close hook, outside the request context)."""
    from statlee.extensions import db
    from statlee.models import User

    cfg = app.config['STATLEE']
    cfg.billing_enabled = True
    billing.reset_spend()
    try:
        post_json(client, '/register', {'email': 'dc@example.com',
                                        'password': 'password123'})
        with app.app_context():
            user = User.query.filter_by(email='dc@example.com').first()
            user.credits = 5
            db.session.commit()
            uid = user.id
        resp = _chat(client)
        if read_first_event:
            next(iter(resp.response))
        resp.close()
        with app.app_context():
            assert db.session.get(User, uid).credits == 5
    finally:
        cfg.billing_enabled = False
        billing.reset_spend()


def _report(client):
    return post_json(client, '/generate_report',
                     {'output': 'coef=1.2, p=0.01', 'interpretation': 'ok'})


def test_report_disconnect_mid_stream_refunds(client, ledger):
    resp = _report(client)
    it = iter(resp.response)
    next(it)
    resp.close()
    assert ledger == Counter(debit=1, refund=1)


def test_report_completed_is_not_refunded(client, ledger):
    events = sse_events(_report(client))
    assert any(e.get('type') == 'done' for e in events)
    assert ledger == Counter(debit=1)
