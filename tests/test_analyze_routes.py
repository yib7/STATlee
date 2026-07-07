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


def test_chat_malformed_moderation_is_blocked(client, fake_llm):
    """Default-deny: a moderation reply that isn't an explicit pass verdict
    (here, non-JSON) must block rather than fail open."""
    fake_llm.set('moderation', 'sure, that looks fine to me')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data'})
    assert resp.status_code == 403


def test_chat_empty_decision_is_blocked(client, fake_llm):
    """A well-formed JSON object missing a 'pass' decision still blocks."""
    fake_llm.set('moderation', '{"reason": "no decision field"}')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data'})
    assert resp.status_code == 403


def test_moderation_blocked_helper_fails_closed():
    from statlee.routes import moderation_blocked
    assert moderation_blocked('{"decision": "pass"}') == (False, '')
    assert moderation_blocked('not json')[0] is True
    assert moderation_blocked('{"decision": "block", "reason": "malware"}') == (
        True, 'malware')
    assert moderation_blocked('')[0] is True
    assert moderation_blocked('{"decision": "maybe"}')[0] is True
    # P1-6: `decision` is matched case-insensitively on purpose — a differently
    # cased "pass" must still pass (safe direction), and a non-pass token still
    # blocks regardless of case.
    assert moderation_blocked('{"decision": "Pass"}') == (False, '')
    assert moderation_blocked('{"decision": "PASS"}') == (False, '')
    assert moderation_blocked('{"decision": "Block", "reason": "x"}') == (True, 'x')


def test_run_guard_blocks_malformed_code_moderation(client, fake_llm):
    """An unparseable code-moderation verdict on an edited script fails closed."""
    fake_llm.set('validation', "print('original')")
    upload_csv(client, SAMPLE_CSV)
    _generate_script(client)
    fake_llm.set('code_moderation', 'looks safe enough')
    resp = post_json(client, '/run',
                     {'filename': 'test.csv',
                      'code': "import os  # edited", 'language': 'Python'})
    assert resp.status_code == 403
    assert 'safety check' in resp.get_json()['error'].lower()


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


def test_chat_pro_mode_routes_codegen_to_pro_max(client, fake_llm):
    """Pro mode runs code generation (the draft) on the bigger 'pro_max' model;
    the other pipeline steps stay on their normal cheap roles."""
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data',
                      'pro': True})
    sse_events(resp)
    # FakeLLMService records (role, kind, text); the code-gen call must use pro_max.
    roles = {c[1]: c[0] for c in fake_llm.calls}
    assert roles.get('draft') == 'pro_max'
    assert roles.get('validation') == 'lite'   # cleanup pass unaffected


def test_chat_default_uses_draft_model(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'describe the data'})
    sse_events(resp)
    roles = {c[1]: c[0] for c in fake_llm.calls}
    assert roles.get('draft') == 'draft'       # default code-gen, not pro_max
    assert all(c[0] != 'pro_max' for c in fake_llm.calls)


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
