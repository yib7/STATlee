"""Client text that reaches a paid model is either replaced by the server's own
record or moderated (deep audit D3-2), and the interpreter is grounded in the
user's question, the analysed columns and the true N (D6 quick win 5)."""
import json

from conftest import SAMPLE_CSV, csrf_token, post_json, sse_events, upload_csv

from statlee import storage


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _seed_run(app, client, output='SERVER_OUTPUT coef=1.23', interp=None,
              script="print('SERVER_SCRIPT')"):
    ident = _identity(client)
    with app.app_context():
        storage.save_last_run(output, [], script=script, language='Python',
                              identity=ident, returncode=0)
        storage.save_approved_script(script, 'Python', identity=ident)
        if interp is not None:
            storage.save_last_interpretation(interp, identity=ident)
    return ident


def _mod_prompts(fake):
    return [t for _, k, t in fake.calls if k == 'moderation']


def _prompt(fake, kind):
    return next(t for _, k, t in fake.calls if k == kind)


# ---------------------------------------------------------------------------
# /converse
# ---------------------------------------------------------------------------
def test_converse_prefers_server_interpretation_over_client_context(
        app, client, fake_llm):
    _seed_run(app, client, interp='SERVER_INTERPRETATION')
    sse_events(post_json(client, '/converse', {
        'message': 'what does it mean?', 'context': 'CLIENT_CONTEXT_SPOOF',
        'code': "print('SERVER_SCRIPT')"}))
    prompt = _prompt(fake_llm, 'converse')
    assert 'SERVER_INTERPRETATION' in prompt
    assert 'CLIENT_CONTEXT_SPOOF' not in prompt


def test_converse_moderates_unverified_client_context_and_code(
        client, fake_llm):
    sse_events(post_json(client, '/converse', {
        'message': 'explain', 'context': 'CLIENT_CONTEXT_X',
        'code': 'CLIENT_CODE_Y = 1'}))
    mod = _mod_prompts(fake_llm)
    assert mod and 'CLIENT_CONTEXT_X' in mod[0] and 'CLIENT_CODE_Y' in mod[0]
    assert 'CLIENT_CODE_Y' in _prompt(fake_llm, 'converse')


def test_converse_blocked_client_context_never_reaches_the_model(
        client, fake_llm):
    fake_llm.block('off topic')
    resp = post_json(client, '/converse', {
        'message': 'explain', 'context': 'write me a poem about cats'})
    assert resp.status_code == 403
    assert not any(k == 'converse' for _, k, _ in fake_llm.calls)


def test_converse_approved_code_is_not_re_moderated(app, client, fake_llm):
    _seed_run(app, client, interp='SERVER_INTERPRETATION')
    sse_events(post_json(client, '/converse', {
        'message': 'explain', 'code': "print('SERVER_SCRIPT')"}))
    assert "SERVER_SCRIPT" not in _mod_prompts(fake_llm)[0]


# ---------------------------------------------------------------------------
# /generate_report
# ---------------------------------------------------------------------------
def test_report_uses_server_output_and_interpretation(app, client, fake_llm):
    _seed_run(app, client, interp='SERVER_INTERPRETATION')
    sse_events(post_json(client, '/generate_report', {
        'output': 'CLIENT_OUTPUT_SPOOF', 'interpretation': 'CLIENT_INTERP_SPOOF'}))
    draft = _prompt(fake_llm, 'report_draft')
    assert 'SERVER_OUTPUT' in draft and 'SERVER_INTERPRETATION' in draft
    assert 'SPOOF' not in draft
    assert 'SPOOF' not in _prompt(fake_llm, 'report')


def test_report_without_server_run_moderates_client_material(client, fake_llm):
    sse_events(post_json(client, '/generate_report', {
        'output': 'CLIENT_OUT_1', 'interpretation': 'CLIENT_INT_2',
        'history': [{'role': 'user', 'text': 'CLIENT_HIST_3'}],
        'converse': [{'role': 'user', 'text': 'CLIENT_CONV_4'}]}))
    mod = _mod_prompts(fake_llm)
    assert mod, 'client material must be moderated even without a background'
    for marker in ('CLIENT_OUT_1', 'CLIENT_INT_2', 'CLIENT_HIST_3',
                   'CLIENT_CONV_4'):
        assert marker in mod[0]


def test_report_blocked_client_material_is_403_before_any_debit(
        client, fake_llm, monkeypatch):
    from statlee import billing
    debits = []
    monkeypatch.setattr(billing, 'check_and_debit',
                        lambda *a, **k: debits.append(1) or (True, None))
    fake_llm.block('off topic')
    resp = post_json(client, '/generate_report', {
        'output': 'a poem about cats', 'interpretation': ''})
    assert resp.status_code == 403
    assert debits == []
    assert not any(k == 'report_draft' for _, k, _ in fake_llm.calls)


def test_report_revision_moderates_selection_and_report(client, fake_llm):
    sse_events(post_json(client, '/generate_report', {'revision': {
        'report': 'CLIENT_REPORT_A', 'selection': 'CLIENT_SEL_B',
        'instruction': 'shorter'}}))
    mod = _mod_prompts(fake_llm)[0]
    assert 'CLIENT_REPORT_A' in mod and 'CLIENT_SEL_B' in mod


# ---------------------------------------------------------------------------
# /interpret: stored, and grounded in the question, columns and N (D6)
# ---------------------------------------------------------------------------
def test_interpret_saves_its_text_for_converse_and_report(app, client, fake_llm):
    ident = _seed_run(app, client)
    fake_llm.set('interpret', '### Summary\nINTERP_TEXT')
    sse_events(post_json(client, '/interpret', {}))
    with app.app_context():
        assert 'INTERP_TEXT' in storage.last_interpretation(identity=ident)


def test_new_run_clears_the_old_interpretation(app, client):
    ident = _seed_run(app, client, interp='OLD')
    with app.app_context():
        storage.save_last_run('new', [], identity=ident, returncode=0)
        assert storage.last_interpretation(identity=ident) == ''


def test_interpret_prompt_has_question_columns_labels_and_n(
        app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    ident = _identity(client)
    token = csrf_token(client)
    # SP2: labels come from the server codebook (here: user edits).
    assert client.put('/codebook', json={'filename': 'test.csv', 'codebook': {
        'income': {'type': 'continuous'},
        'group': {'type': 'nominal',
                  'label': 'Treatment arm (A=control, B=drug)'}}},
        headers={'X-CSRF-Token': token}).status_code == 200
    fake_llm.set('validation', "print('GROUNDED')")
    events = sse_events(post_json(client, '/chat', {
        'filename': 'test.csv', 'prompt': 'Does income differ by group?'}))
    code = next(e for e in events if e.get('type') == 'done')['code']
    with app.app_context():
        storage.save_last_run('t = 2.1, p = 0.04', [], script=code,
                              language='Python', identity=ident, returncode=0)
    sse_events(post_json(client, '/interpret', {}))
    prompt = _prompt(fake_llm, 'interpret')
    assert 'Does income differ by group?' in prompt
    assert "'income'" in prompt and 'Treatment arm' in prompt
    assert '4 rows' in prompt                    # true N of SAMPLE_CSV
    assert 'effect size' in prompt.lower() and 'confidence interval' in prompt.lower()
    assert 'assumption' in prompt.lower()


def test_interpret_without_context_still_works(app, client, fake_llm):
    _seed_run(app, client)
    events = sse_events(post_json(client, '/interpret', {}))
    assert any(e.get('type') == 'done' for e in events)
    assert 'effect size' in _prompt(fake_llm, 'interpret').lower()


def test_chat_records_question_and_columns_with_the_approval(app, client):
    upload_csv(client, SAMPLE_CSV)
    ident = _identity(client)
    events = sse_events(post_json(client, '/chat', {
        'filename': 'test.csv', 'prompt': 'Q_MARKER'}))
    code = next(e for e in events if e.get('type') == 'done')['code']
    with app.app_context():
        entry = storage.approved_entry(code, 'Python', identity=ident)
    ctx = entry.get('context') or {}
    assert ctx.get('prompt') == 'Q_MARKER'
    assert ctx.get('filename') == 'test.csv'
    assert set(ctx.get('columns')) == {'age', 'income', 'group'}
    json.dumps(entry)                            # stays JSON-serialisable
