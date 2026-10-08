"""Routes turn provider safety blocks, refusals and empty output into a clear
user-facing message (deep audit D3-4). On /chat an empty or blocked result is
never saved as an approved script and the credit comes back."""
from collections import Counter

import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import billing, llm


@pytest.fixture
def ledger(monkeypatch):
    calls = Counter()
    monkeypatch.setattr(billing, 'check_and_debit',
                        lambda *a, **k: calls.update(['debit']) or (True, None))
    monkeypatch.setattr(billing, 'refund',
                        lambda *a, **k: calls.update(['refund']))
    return calls


def _blocked():
    return llm.BlockedGenerationError('Gemini', 'm', detail='SAFETY')


def _chat(client, **extra):
    upload_csv(client, SAMPLE_CSV)
    payload = {'filename': 'test.csv', 'prompt': 'summarize', 'pro': True}
    payload.update(extra)
    return post_json(client, '/chat', payload)


def _errors(events):
    return [e for e in events if e.get('type') == 'error']


@pytest.mark.parametrize('kind', ['draft', 'validation'])
def test_chat_blocked_stream_refunds_and_explains(client, fake_llm, ledger, kind):
    fake_llm.fail(kind, _blocked())
    events = sse_events(_chat(client))
    errs = _errors(events)
    assert errs and "safety filter" in errs[-1]['message']
    assert errs[-1]['reason'] == 'blocked'
    assert not any(e.get('type') == 'done' for e in events)
    assert ledger == Counter(debit=1, refund=1)
    assert not any(k == 'code_moderation' for _, k, _ in fake_llm.calls)


def test_chat_empty_validation_is_not_approved_and_refunds(
        client, fake_llm, ledger):
    fake_llm.set('validation', '')
    events = sse_events(_chat(client))
    assert _errors(events)
    assert not any(e.get('type') == 'done' for e in events)
    assert ledger == Counter(debit=1, refund=1)
    # nothing was moderated as a script, so nothing can have been approved
    assert not any(k == 'code_moderation' for _, k, _ in fake_llm.calls)


def test_chat_fence_only_validation_counts_as_empty(client, fake_llm, ledger):
    fake_llm.set('validation', '```python\n```')
    events = sse_events(_chat(client))
    assert _errors(events)
    assert ledger == Counter(debit=1, refund=1)


def test_chat_truncated_stream_explains_the_cause(client, fake_llm, ledger):
    fake_llm.fail('draft', llm.TruncatedGenerationError('Gemini', 'm'))
    errs = _errors(sse_events(_chat(client)))
    assert 'output-token limit' in errs[-1]['message']
    assert errs[-1]['reason'] == 'truncated'


def test_chat_moderation_blocked_by_provider_is_403(client, fake_llm, ledger):
    fake_llm.fail('moderation', _blocked())
    resp = _chat(client)
    assert resp.status_code == 403
    assert 'safety filter' in resp.get_json()['error']
    assert ledger == Counter()          # denied before any debit


def test_converse_blocked_stream_explains(client, fake_llm):
    fake_llm.fail('converse', _blocked())
    events = sse_events(post_json(client, '/converse', {'message': 'why?'}))
    assert 'safety filter' in _errors(events)[-1]['message']


def test_interpret_blocked_stream_explains(client, fake_llm):
    fake_llm.fail('interpret', _blocked())
    events = sse_events(post_json(client, '/interpret', {
        'output': 'coef 1.2 p=0.01', 'success': True}))
    assert 'safety filter' in _errors(events)[-1]['message']


def test_report_blocked_draft_refunds_and_explains(client, fake_llm, ledger):
    fake_llm.fail('report_draft', _blocked())
    events = sse_events(post_json(client, '/generate_report', {
        'output': 'coef 1.2', 'interpretation': 'strong'}))
    assert 'safety filter' in _errors(events)[-1]['message']
    assert ledger == Counter(debit=1, refund=1)


def test_suggest_blocked_is_422_with_message(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.fail('suggest', _blocked())
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert resp.status_code == 422
    assert 'safety filter' in resp.get_json()['error']


def test_wrangle_blocked_plan_is_422(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.fail('wrangle', _blocked())
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop group'})
    assert resp.status_code == 422
    assert 'safety filter' in resp.get_json()['error']


def test_run_code_moderation_blocked_by_provider_is_403(client, fake_llm):
    fake_llm.fail('code_moderation', _blocked())
    resp = post_json(client, '/run', {'code': "print('x')",
                                      'language': 'Python'})
    assert resp.status_code == 403
    assert 'safety filter' in resp.get_json()['error']


def test_empty_moderation_reply_still_fails_closed(client, fake_llm):
    fake_llm.fail('moderation', llm.EmptyGenerationError('Gemini', 'm'))
    resp = post_json(client, '/converse', {'message': 'why?'})
    assert resp.status_code == 403
