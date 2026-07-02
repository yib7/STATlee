"""Converse guardrails + moderation (0.6/4.2/5.15), report builder (5.17),
issue reporting (6.3), and metrics usage accounting (3.3/3.4)."""
from conftest import post_json, sse_events


def test_converse_moderation_gate(client, fake_llm):
    """0.6: /converse now has the same moderation gate as /chat."""
    fake_llm.block('Off-topic')
    resp = post_json(client, '/converse', {'message': 'write me a poem'})
    assert resp.status_code == 403


def test_converse_malformed_moderation_is_blocked(client, fake_llm):
    """Default-deny applies to /converse too."""
    fake_llm.set('moderation', 'yep go ahead')
    resp = post_json(client, '/converse', {'message': 'what is a t-test?'})
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


def test_generate_report_is_two_pass_pro_then_flash(client, fake_llm):
    """The draft is compiled on the bigger 'pro_max' (3.1-pro) model, then the
    finished paper is written on 'pro' (3.5-flash). The converse discussion of
    the findings is folded into the draft pass."""
    resp = post_json(client, '/generate_report',
                     {'output': 'coef=1.2 p=0.01', 'interpretation': 'significant',
                      'converse': [{'role': 'user', 'text': 'is this causal?'}]})
    sse_events(resp)
    by_kind = {c[1]: c for c in fake_llm.calls}
    assert by_kind['report_draft'][0] == 'pro_max'   # draft on the bigger model
    assert by_kind['report'][0] == 'pro'             # final paper on flash
    assert 'is this causal?' in by_kind['report_draft'][2]


def test_generate_report_essay_format_reaches_prompt(client, fake_llm):
    resp = post_json(client, '/generate_report',
                     {'output': 'coef=1.2', 'interpretation': 'ok',
                      'format': 'essay'})
    sse_events(resp)
    final = next(c for c in fake_llm.calls if c[1] == 'report')
    assert 'traditional essay' in final[2]


def test_generate_report_default_format_is_sectioned(client, fake_llm):
    resp = post_json(client, '/generate_report',
                     {'output': 'coef=1.2', 'interpretation': 'ok'})
    sse_events(resp)
    final = next(c for c in fake_llm.calls if c[1] == 'report')
    assert 'formal data analysis report' in final[2]


def test_generate_report_rejects_non_dict_revision(client):
    """P1-5: a truthy non-dict 'revision' must return a structured 400, not 500."""
    resp = post_json(client, '/generate_report', {'revision': 'yes'})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


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


def test_footer_attributes_default_provider(client):
    # Default config is the Gemini provider; the footer attributes it.
    assert 'Google Gemini' in client.get('/').get_data(as_text=True)
    assert 'Google Gemini' in client.get('/welcome').get_data(as_text=True)


def test_metrics_reflects_llm_usage(client, fake_llm):
    # Drive one converse call, then confirm usage shows up in /metrics.
    post_json(client, '/converse', {'message': 'explain correlation'})
    usage = client.get('/metrics').get_json()['llm_usage']
    assert usage                      # at least one model/role recorded
    assert any(v['calls'] >= 1 for v in usage.values())
