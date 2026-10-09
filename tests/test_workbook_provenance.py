"""SP1 fix round 1: an older run is interpreted with ITS question, columns
and dataset version (not the newest generation's), and a run's recorded
prompt is never silently borrowed from another generation."""
import pandas as pd
from conftest import SAMPLE_CSV, csrf_token, post_json, sse_events, upload_csv

from statlee import storage


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _generate(client, fake_llm, code, prompt):
    fake_llm.set('validation', code)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': prompt}))


def _run(client, code):
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['run_id']


def _interpret_prompt(fake_llm):
    return [t for _, k, t in fake_llm.calls if k == 'interpret'][-1]


def _meta(app, client, rid):
    with app.app_context():
        return storage.run_meta(rid, identity=_identity(client))


def test_interpret_older_run_uses_its_own_question(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, "print('A')", 'QUESTION_A about income')
    run_a = _run(client, "print('A')")
    for i in range(6):           # more than APPROVED_SCRIPT_MAX generations
        _generate(client, fake_llm, f"print({i})", f'OTHER_QUESTION_{i}')
        _run(client, f"print({i})")
    with app.app_context():
        assert storage.approved_entry("print('A')", 'Python',
                                      identity=_identity(client)) is None
    fake_llm.calls.clear()
    sse_events(post_json(client, '/interpret', {'run_id': run_a}))
    prompt = _interpret_prompt(fake_llm)
    assert 'QUESTION_A about income' in prompt
    assert 'OTHER_QUESTION_5' not in prompt
    # Columns are only taken from an approval whose question matches; the
    # evicted approval leaves none, so no other generation's columns appear.
    assert 'ANALYSED COLUMNS' not in prompt


def test_interpret_uses_the_runs_dataset_version(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, "print('V1')", 'Q on v1')
    run_v1 = _run(client, "print('V1')")
    ident = _identity(client)
    with app.app_context():
        df = pd.DataFrame({'age': [1, 2], 'income': [3, 4], 'group': ['A', 'B']})
        storage.add_dataset_version('test.csv', df, 'shrink', identity=ident)
    fake_llm.calls.clear()
    sse_events(post_json(client, '/interpret', {'run_id': run_v1}))
    prompt = _interpret_prompt(fake_llm)
    assert '4 rows' in prompt and '2 rows' not in prompt
    assert 'Q on v1' in prompt


def test_interpret_omits_size_when_the_runs_data_is_gone(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, "print('GONE')", 'Q gone')
    rid = _run(client, "print('GONE')")
    # A same-name re-upload replaces v1 with different bytes.
    upload_csv(client, "a,b\n1,2\n3,4\n5,6\n7,8\n9,10\n")
    fake_llm.calls.clear()
    sse_events(post_json(client, '/interpret', {'run_id': rid}))
    prompt = _interpret_prompt(fake_llm)
    assert 'Q gone' in prompt
    assert 'DATASET SIZE' not in prompt


def test_rerun_after_eviction_keeps_the_original_runs_prompt(
        app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, "print('ORIG')", 'ORIGINAL_QUESTION')
    first = _run(client, "print('ORIG')")
    for i in range(6):
        _generate(client, fake_llm, f"print({i})", f'LATER_{i}')
    again = _run(client, "print('ORIG')")   # approval evicted: re-moderated
    meta, first_meta = _meta(app, client, again), _meta(app, client, first)
    assert meta['prompt'] == 'ORIGINAL_QUESTION'
    assert meta['role'] == first_meta['role'] == 'draft'
    assert meta['model'] == first_meta['model']
    assert meta['prompt_inherited'] is False


def test_edited_script_prompt_is_marked_inherited(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, "print('GEN')", 'GEN_QUESTION')
    rid = _run(client, "print('GEN')  # edited")
    meta = _meta(app, client, rid)
    assert meta['prompt'] == 'GEN_QUESTION'
    assert meta['prompt_inherited'] is True
    assert meta['role'] is None and meta['model'] is None


def test_generated_run_prompt_is_not_inherited(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _generate(client, fake_llm, "print('OWN')", 'OWN_QUESTION')
    meta = _meta(app, client, _run(client, "print('OWN')"))
    assert meta['prompt'] == 'OWN_QUESTION'
    assert meta['prompt_inherited'] is False
    body = client.get(f"/runs/{meta['run_id']}").get_json()
    assert body['prompt_inherited'] is False


def test_patch_returns_404_when_run_vanishes(app, client, monkeypatch):
    with app.app_context():
        rid = storage.save_run('x', [], identity=_identity(client))
    monkeypatch.setattr(storage, 'run_summary', lambda *a, **k: None)
    resp = client.patch(f'/runs/{rid}', json={'starred': True},
                        headers={'X-CSRF-Token': csrf_token(client)})
    assert resp.status_code == 404


def test_run_records_the_version_it_read_when_a_wrangle_lands_mid_run(
        app, client, fake_llm, monkeypatch):
    """Final review item 3: version and hash are snapshotted before the
    sandbox starts, so a wrangle committed during the run cannot pair
    version 2 with version 1's bytes."""
    import hashlib

    from statlee import sandbox
    upload_csv(client, SAMPLE_CSV)
    ident = _identity(client)
    with app.app_context():
        v1_path = storage.active_dataset_path('test.csv', identity=ident)
    with open(v1_path, 'rb') as f:
        v1_sha = hashlib.sha256(f.read()).hexdigest()
    real = sandbox.run_in_sandbox

    def wrangle_during_run(*args, **kwargs):
        result = real(*args, **kwargs)
        storage.add_dataset_version(
            'test.csv', pd.DataFrame({'age': [1]}), 'mid-run edit',
            identity=ident, code='df = df.head(1)')
        return result
    monkeypatch.setattr(sandbox, 'run_in_sandbox', wrangle_during_run)
    _generate(client, fake_llm, "print('MID')", 'Q mid')
    rid = _run(client, "print('MID')")
    meta = _meta(app, client, rid)
    with app.app_context():
        assert storage.active_version_number('test.csv', identity=ident) == 2
    assert meta['dataset_version'] == 1
    assert meta['dataset_sha256'] == v1_sha
