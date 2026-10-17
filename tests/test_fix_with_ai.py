"""Fix with AI keeps the user's question (2026-10-17).

"Fix with AI" sends the failing run's traceback to /chat as the prompt. That
debug text is what the model needs, but it is not the user's question: the
repaired run used to be saved (run rail title, /runs, /interpret grounding,
provenance.json) under "The script failed in the sandbox with this
error: ...". A fix request names the run it repairs (``fix_of``) and the
server records that run's question instead.
"""
import io
import json
import zipfile

from conftest import SAMPLE_CSV, csrf_token, post_json, sse_events, upload_csv

from statlee import storage

QUESTION = 'Does income predict turnout? Use logistic regression and plot it.'
BROKEN = "raise ValueError('boom')"
FIXED = "print('fixed output')"
DEBUG_PROMPT = ('The script failed in the sandbox with this error:\n\n'
                'ValueError: boom\n\nFix the cause and return the corrected script.')


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _generate(client, fake_llm, code, prompt, **extra):
    fake_llm.set('validation', code)
    body = {'filename': 'test.csv', 'prompt': prompt}
    body.update(extra)
    return sse_events(post_json(client, '/chat', body))


def _run(client, code):
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _failed_then_fixed(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, BROKEN, QUESTION)
    first = _run(client, BROKEN)
    assert not first['success']
    events = _generate(client, fake_llm, FIXED, DEBUG_PROMPT,
                       current_code=BROKEN, fix_of=first['run_id'],
                       history=[{'role': 'user', 'text': QUESTION},
                                {'role': 'model', 'text': BROKEN}])
    second = _run(client, FIXED)
    assert second['success'], second['output']
    return first['run_id'], second['run_id'], events


def test_fixed_run_keeps_the_original_question(app, client, fake_llm):
    first, second, events = _failed_then_fixed(client, fake_llm)
    with app.app_context():
        meta = storage.run_meta(second, identity=_identity(client))
    assert meta['prompt'] == QUESTION
    assert meta['fix_of'] == first
    assert meta['prompt_inherited'] is False
    # The done event hands the question back for the client's /history entry.
    done = [e for e in events if e.get('type') == 'done'][-1]
    assert done['question'] == QUESTION


def test_fix_prompt_still_reaches_the_model(app, client, fake_llm):
    _failed_then_fixed(client, fake_llm)
    drafts = [t for _, k, t in fake_llm.calls if k == 'draft']
    assert 'ValueError: boom' in drafts[-1]


def test_run_rail_and_run_view_show_the_question_and_fix_marker(
        app, client, fake_llm):
    first, second, _ = _failed_then_fixed(client, fake_llm)
    rows = {r['run_id']: r for r in client.get('/runs').get_json()['runs']}
    assert rows[second]['prompt_excerpt'] == QUESTION
    assert rows[second]['fix_of'] == first
    assert rows[first]['fix_of'] is None
    body = client.get(f'/runs/{second}').get_json()
    assert body['prompt'] == QUESTION and body['fix_of'] == first
    assert 'sandbox with this error' not in json.dumps(body['prompt'])


def test_interpret_of_the_fixed_run_grounds_on_the_question(
        app, client, fake_llm):
    _, second, _ = _failed_then_fixed(client, fake_llm)
    fake_llm.calls.clear()
    sse_events(post_json(client, '/interpret', {'run_id': second}))
    prompt = [t for _, k, t in fake_llm.calls if k == 'interpret'][-1]
    assert QUESTION in prompt
    assert 'sandbox with this error' not in prompt


def test_provenance_records_the_question_and_the_fix(app, client, fake_llm):
    first, second, _ = _failed_then_fixed(client, fake_llm)
    resp = post_json(client, '/export', {'filename': 'test.csv',
                                         'language': 'Python', 'runs': 'all'})
    assert resp.status_code == 200, resp.data[:300]
    prov = json.loads(zipfile.ZipFile(io.BytesIO(resp.data))
                      .read('provenance.json').decode('utf-8'))
    runs = {s['run_id']: s for s in prov['steps'] if s['kind'] == 'run'}
    assert runs[second]['prompt'] == QUESTION
    assert runs[second]['fix_of'] == first
    assert 'fix_of' not in runs[first]
    assert 'sandbox with this error' not in json.dumps(prov)


def test_fix_of_an_unknown_run_never_titles_the_run_with_the_debug_text(
        app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    events = _generate(client, fake_llm, FIXED, DEBUG_PROMPT, fix_of='r0999')
    assert [e for e in events if e.get('type') == 'done']
    rid = _run(client, FIXED)['run_id']
    with app.app_context():
        meta = storage.run_meta(rid, identity=_identity(client))
    assert meta['prompt'] is None
    assert meta['fix_of'] is None


def test_fix_of_must_be_a_string(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': DEBUG_PROMPT, 'fix_of': 7})
    assert resp.status_code == 400


def test_an_ordinary_generation_records_its_own_prompt(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    events = _generate(client, fake_llm, FIXED, 'Plain question')
    assert [e for e in events if e.get('type') == 'done'][-1]['question'] \
        == 'Plain question'
    rid = _run(client, FIXED)['run_id']
    with app.app_context():
        meta = storage.run_meta(rid, identity=_identity(client))
    assert meta['prompt'] == 'Plain question' and meta['fix_of'] is None
