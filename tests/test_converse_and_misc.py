"""Converse guardrails + moderation (0.6/4.2/5.15), report builder (5.17),
issue reporting (6.3), and metrics usage accounting (3.3/3.4)."""
from conftest import post_json, sse_events


def test_converse_moderation_gate(client, fake_llm):
    """0.6: /converse now has the same moderation gate as /chat."""
    fake_llm.block('Off-topic')
    resp = post_json(client, '/converse', {'message': 'write me a poem'})
    assert resp.status_code == 403


def test_converse_single_pass_stream(client):
    resp = post_json(client, '/converse',
                     {'message': 'what does this p-value mean?'})
    assert resp.status_code == 200
    events = sse_events(resp)
    text = ''.join(e['text'] for e in events if e.get('type') == 'delta')
    assert text                       # got a streamed answer
    assert any(e.get('type') == 'done' for e in events)


def test_converse_guide_mode_uses_guide_persona(client, fake_llm):
    fake_llm.set('converse_guide', 'Frame it as income predicts turnout.')
    resp = post_json(client, '/converse',
                     {'message': 'I think income affects voting', 'mode': 'guide'})
    events = sse_events(resp)
    text = ''.join(e['text'] for e in events if e.get('type') == 'delta')
    assert 'predicts' in text
    # The guide persona prompt was the one actually sent.
    assert any(c[1] == 'converse_guide' for c in fake_llm.calls)


def test_converse_requires_message(client):
    resp = post_json(client, '/converse', {'message': '   '})
    assert resp.status_code == 400


def test_generate_report_requires_grounding(client):
    resp = post_json(client, '/generate_report',
                     {'output': '', 'interpretation': ''})
    assert resp.status_code == 422


def test_generate_report_streams_markdown(client, fake_llm):
    fake_llm.set('report', '# Findings\n\nThe effect is robust.')
    resp = post_json(client, '/generate_report',
                     {'output': 'coef=1.2 p=0.01',
                      'interpretation': 'significant', 'length': 'short'})
    events = sse_events(resp)
    text = ''.join(e['text'] for e in events if e.get('type') == 'delta')
    assert 'Findings' in text


def test_generate_report_revision(client, fake_llm):
    fake_llm.set('report_revision', 'A tighter paragraph.')
    resp = post_json(client, '/generate_report', {'revision': {
        'report': '# R\n\nLong paragraph.', 'selection': 'Long paragraph.',
        'instruction': 'make it concise'}})
    events = sse_events(resp)
    text = ''.join(e['text'] for e in events if e.get('type') == 'delta')
    assert 'tighter' in text


def test_report_issue_persists(client):
    resp = post_json(client, '/report_issue',
                     {'description': 'The run button hangs.',
                      'code': "print('x')", 'output': 'stuck',
                      'console_errors': 'TypeError'})
    body = resp.get_json()
    assert body['status'] == 'success'
    assert isinstance(body['id'], int)


def test_report_issue_requires_description(client):
    resp = post_json(client, '/report_issue', {'description': ''})
    assert resp.status_code == 400


def test_metrics_reflects_llm_usage(client, fake_llm):
    # Drive one converse call, then confirm usage shows up in /metrics.
    post_json(client, '/converse', {'message': 'explain correlation'})
    usage = client.get('/metrics').get_json()['llm_usage']
    assert usage                      # at least one model/role recorded
    assert any(v['calls'] >= 1 for v in usage.values())
