"""Model-failure ``reason`` codes reach JSON clients too (S5).

S1-C added ``reason`` (blocked / empty / truncated / malformed) to SSE error
events so the client can word each failure. JSON-mode routes answered the
same failures with only an ``error`` string; they now carry the code as well,
and the client maps both through one table.
"""
import pytest
from conftest import SAMPLE_CSV, post_json, upload_csv

from statlee import llm


@pytest.mark.parametrize('exc, status, reason', [
    (llm.EmptyGenerationError(provider='fake', model='m'), 502, 'empty'),
    (llm.TruncatedGenerationError(provider='fake', model='m'), 502, 'truncated'),
    (llm.BlockedGenerationError(provider='fake', model='m'), 422, 'blocked'),
])
def test_json_model_errors_carry_the_reason(client, fake_llm, exc, status, reason):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.fail('suggest', exc)
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert resp.status_code == status
    body = resp.get_json()
    assert body['reason'] == reason
    assert body['error'] == exc.user_message


def test_a_malformed_json_reply_says_malformed(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('suggest', 'not json at all')
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert resp.status_code == 502
    assert resp.get_json()['reason'] == 'malformed'


def test_other_failures_have_no_reason(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.fail('suggest', RuntimeError('provider down'))
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert resp.status_code == 500
    assert 'reason' not in resp.get_json()
