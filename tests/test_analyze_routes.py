"""Analysis pipeline routes: /chat moderation + streamed draft/validation
(5.5), the /run run-guard (0.4), and /interpret incl. auto-debug (5.11)."""
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv


def test_chat_blocked_by_moderation(client, fake_llm):
    fake_llm.block('Safety Violation')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'build malware'})
    assert resp.status_code == 403
    assert 'denied' in resp.get_json()['error'].lower()


def test_chat_streams_phases_and_final_code(client, fake_llm):
    fake_llm.set('validation', "print('final code')")
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data'})
    assert resp.status_code == 200
    events = sse_events(resp)
    phases = [e['phase'] for e in events if e.get('type') == 'phase']
    assert phases == ['drafting', 'validating']
    done = [e for e in events if e.get('type') == 'done']
    assert done and done[0]['code'] == "print('final code')"


def test_chat_propagates_priority_flag_to_llm(client, fake_llm):
    """The UI's priority toggle reaches every model call in the pipeline."""
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data',
                      'priority': True})
    sse_events(resp)
    # FakeLLMService records (role, kind, text, priority); the draft + validation
    # streamed calls must carry priority=True.
    kinds = {c[1]: c[3] for c in fake_llm.calls}
    assert kinds.get('draft') is True
    assert kinds.get('validation') is True


def test_chat_defaults_to_non_priority(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data'})
    sse_events(resp)
    assert all(c[3] is False for c in fake_llm.calls)


def _generate_script(client, prompt='go'):
    """Run /chat and DRAIN the SSE stream so the generator persists the
    approved script (it only saves once the body is consumed)."""
    resp = post_json(client, '/chat', {'filename': 'test.csv', 'prompt': prompt})
    sse_events(resp)
    return resp


def test_chat_saves_approved_script_for_run_guard(client, fake_llm):
    fake_llm.set('validation', "print('approved body')")
    upload_csv(client, SAMPLE_CSV)
    _generate_script(client, 'summarize')
    # Running the EXACT approved script executes without a re-moderation call.
    run = post_json(client, '/run',
                    {'filename': 'test.csv', 'code': "print('approved body')",
                     'language': 'Python'})
    body = run.get_json()
    assert body['success'] is True
    assert 'approved body' in body['output']


def test_run_without_generation_is_blocked(client):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/run',
                     {'filename': 'test.csv', 'code': "print('x')",
                      'language': 'Python'})
    assert resp.status_code == 403
    assert 'Generate code' in resp.get_json()['error']


def test_run_guard_remoderates_edited_script(client, fake_llm):
    fake_llm.set('validation', "print('original')")
    upload_csv(client, SAMPLE_CSV)
    _generate_script(client)

    # Edited script + a BLOCK verdict from the code-moderation gate -> 403.
    fake_llm.block_code('network access')
    resp = post_json(client, '/run',
                     {'filename': 'test.csv',
                      'code': "import requests  # edited", 'language': 'Python'})
    assert resp.status_code == 403
    assert 'safety check' in resp.get_json()['error'].lower()


def test_run_guard_allows_clean_edit(client, fake_llm):
    fake_llm.set('validation', "print('original')")
    upload_csv(client, SAMPLE_CSV)
    _generate_script(client)

    # Edited but PASSes code-moderation (default) -> runs.
    resp = post_json(client, '/run',
                     {'filename': 'test.csv',
                      'code': "print('edited-and-clean')", 'language': 'Python'})
    body = resp.get_json()
    assert body['success'] is True
    assert 'edited-and-clean' in body['output']


def test_interpret_streams_summary(client):
    resp = post_json(client, '/interpret',
                     {'output': 'p = 0.03', 'success': True, 'plots': []})
    events = sse_events(resp)
    text = ''.join(e['text'] for e in events if e.get('type') == 'delta')
    assert 'significant' in text.lower()
    done = [e for e in events if e.get('type') == 'done']
    assert done and done[0]['debug'] is False


def test_interpret_switches_to_debug_on_failure(client, fake_llm):
    fake_llm.set('interpret_debug', '### What went wrong\nA NameError.')
    resp = post_json(client, '/interpret',
                     {'output': 'Traceback (most recent call last): NameError',
                      'success': False, 'code': "print(x)"})
    events = sse_events(resp)
    text = ''.join(e['text'] for e in events if e.get('type') == 'delta')
    assert 'went wrong' in text.lower()
    done = [e for e in events if e.get('type') == 'done']
    assert done and done[0]['debug'] is True


def test_interpret_handles_empty_output(client):
    resp = post_json(client, '/interpret', {'output': '', 'plots': []})
    assert resp.get_json()['interpretation'].startswith('No statistical output')


def test_method_prompt_drafts_for_dataset(client, fake_llm):
    fake_llm.set('method_prompt',
                 '{"prompt": "Run an OLS of income on age.", "rationale": "ok"}')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/method_prompt',
                     {'filename': 'test.csv',
                      'method': {'name': 'OLS', 'description': 'regression'}})
    body = resp.get_json()
    assert body['status'] == 'success'
    assert 'OLS' in body['prompt']
