"""SP1 session workbook routes: /run returns a run_id, GET/PATCH /runs, and
run selection for /interpret, /converse and /generate_report."""
import base64

from conftest import (
    SAMPLE_CSV,
    csrf_token,
    post_json,
    sse_events,
    upload_csv,
)

from statlee import datatools, storage

PNG = base64.b64encode(b'\x89PNG\r\n\x1a\nfake').decode('ascii')


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _seed(app, client, output, interp=None, plots=(), script=None, **kw):
    ident = _identity(client)
    with app.app_context():
        rid = storage.save_run(output, list(plots), script=script,
                               language='Python', identity=ident,
                               returncode=0, **kw)
        if interp is not None:
            storage.save_interpretation(interp, run_id=rid, identity=ident)
    return rid


def _patch(client, path, payload, token=None):
    headers = {}
    if token is None:
        token = csrf_token(client)
    if token:
        headers['X-CSRF-Token'] = token
    return client.patch(path, json=payload, headers=headers)


def _prompt(fake, kind):
    return next(t for _, k, t in fake.calls if k == kind)


# ---------------------------------------------------------------------------
# /run
# ---------------------------------------------------------------------------

def test_run_returns_run_id_and_records_provenance(app, client, fake_llm):
    fake_llm.set('validation', "print('hello')")
    upload_csv(client, SAMPLE_CSV)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'Describe income'}))
    first = post_json(client, '/run', {'filename': 'test.csv',
                                       'code': "print('hello')",
                                       'language': 'Python'})
    assert first.status_code == 200
    assert first.get_json()['run_id'] == 'r0001'
    second = post_json(client, '/run', {'filename': 'test.csv',
                                        'code': "print('hello')",
                                        'language': 'Python'})
    assert second.get_json()['run_id'] == 'r0002'

    ident = _identity(client)
    with app.app_context():
        meta = storage.load_run('r0001', identity=ident)['meta']
        path = storage.active_dataset_path('test.csv', identity=ident)
        sha = datatools.file_sha256(path)
    cfg = app.config['STATLEE']
    assert meta['prompt'] == 'Describe income'
    assert meta['filename'] == 'test.csv'
    assert meta['dataset_version'] == 1
    assert meta['dataset_sha256'] == sha
    assert meta['role'] == 'draft'
    assert meta['model'] == cfg.model_pro
    # SP3: /run stores the parsed card (this output has no methods block)
    assert meta['methods'] == {'method': None, 'assumptions': [], 'effects': [],
                               'reported': False}


def test_run_pro_mode_records_pro_max_role(app, client, fake_llm):
    fake_llm.set('validation', "print('pro')")
    upload_csv(client, SAMPLE_CSV)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'p', 'pro': True}))
    rid = post_json(client, '/run', {'filename': 'test.csv',
                                     'code': "print('pro')"}).get_json()['run_id']
    with app.app_context():
        meta = storage.load_run(rid, identity=_identity(client))['meta']
    assert meta['role'] == 'pro_max'
    assert meta['model'] == app.config['STATLEE'].model_pro_max


def test_run_without_dataset_has_null_dataset_fields(app, client):
    resp = post_json(client, '/run', {'code': "print(1)"})
    rid = resp.get_json()['run_id']
    with app.app_context():
        meta = storage.load_run(rid, identity=_identity(client))['meta']
    assert meta['filename'] is None
    assert meta['dataset_version'] is None
    assert meta['dataset_sha256'] is None


# ---------------------------------------------------------------------------
# GET /runs, GET /runs/<id>, PATCH /runs/<id>
# ---------------------------------------------------------------------------

def test_list_runs_shape(app, client):
    _seed(app, client, 'one', prompt='A' * 300)
    _seed(app, client, 'two', plots=[PNG])
    body = client.get('/runs').get_json()
    assert body['latest'] == 'r0002'
    assert [r['run_id'] for r in body['runs']] == ['r0001', 'r0002']
    first, second = body['runs']
    assert set(first) >= {'run_id', 'name', 'created_at', 'prompt_excerpt',
                          'starred', 'has_plots', 'returncode'}
    assert len(first['prompt_excerpt']) == 120
    assert first['has_plots'] is False and second['has_plots'] is True
    assert first['starred'] is False and first['name'] is None
    assert first['returncode'] == 0


def test_list_runs_empty(client):
    assert client.get('/runs').get_json() == {'runs': [], 'latest': None}


def test_get_run_returns_full_run_with_plots(app, client):
    rid = _seed(app, client, 'FULL OUTPUT', interp='INTERP', plots=[PNG],
                script="print('S')", prompt='Q', filename='test.csv',
                dataset_version=2, model='m', role='draft')
    body = client.get(f'/runs/{rid}').get_json()
    assert body['run_id'] == rid
    assert body['output'] == 'FULL OUTPUT'
    assert body['interpretation'] == 'INTERP'
    assert body['code'] == "print('S')" and body['language'] == 'Python'
    assert body['plots'] == [PNG]
    assert body['returncode'] == 0 and body['timed_out'] is False
    assert body['prompt'] == 'Q' and body['filename'] == 'test.csv'
    assert body['dataset_version'] == 2 and body['model'] == 'm'
    assert body['role'] == 'draft'
    # SP3: a run saved with methods null is parsed on the fly (no block here)
    assert body['methods'] == {'method': None, 'assumptions': [], 'effects': [],
                               'reported': False}
    assert body['starred'] is False and body['is_latest'] is True


def test_get_unknown_or_malformed_run_is_404(app, client):
    _seed(app, client, 'x')
    for bad in ('r9999', 'r1', 'R0001', 'r0001.json', '..%2Fr0001',
                '%2E%2E', 'r0001%00'):
        assert client.get(f'/runs/{bad}').status_code == 404, bad


def test_foreign_run_is_404(app, client):
    rid = _seed(app, client, 'OWNER SECRET')
    other = app.test_client()
    resp = other.get(f'/runs/{rid}')
    assert resp.status_code == 404
    assert 'OWNER SECRET' not in resp.get_data(as_text=True)
    assert _patch(other, f'/runs/{rid}', {'starred': True}).status_code == 404


def test_patch_run_renames_and_stars(app, client):
    rid = _seed(app, client, 'x')
    resp = _patch(client, f'/runs/{rid}', {'name': '  Main model ',
                                           'starred': True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['run_id'] == rid
    assert body['name'] == 'Main model' and body['starred'] is True
    listed = client.get('/runs').get_json()['runs'][0]
    assert listed['name'] == 'Main model' and listed['starred'] is True
    # Clearing the name.
    assert _patch(client, f'/runs/{rid}', {'name': ''}).get_json()['name'] is None


def test_patch_run_validation(app, client):
    rid = _seed(app, client, 'x')
    assert _patch(client, f'/runs/{rid}', {'name': 'x' * 81}).status_code == 400
    assert _patch(client, f'/runs/{rid}', {'name': 5}).status_code == 400
    assert _patch(client, f'/runs/{rid}', {'starred': 'yes'}).status_code == 400
    assert _patch(client, f'/runs/{rid}', {}).status_code == 400
    assert _patch(client, '/runs/r0404', {'starred': True}).status_code == 404
    assert _patch(client, f'/runs/{rid}', {'name': 'x' * 80}).status_code == 200


def test_patch_run_requires_csrf(app, client):
    rid = _seed(app, client, 'x')
    resp = client.patch(f'/runs/{rid}', json={'starred': True})
    assert resp.status_code == 403
    assert client.get('/runs').get_json()['runs'][0]['starred'] is False


# ---------------------------------------------------------------------------
# /interpret
# ---------------------------------------------------------------------------

def test_interpret_selected_run_grounds_and_stores_on_that_run(
        app, client, fake_llm):
    r1 = _seed(app, client, 'FIRST_RUN_OUTPUT')
    _seed(app, client, 'SECOND_RUN_OUTPUT')
    events = sse_events(post_json(client, '/interpret', {'run_id': r1}))
    assert events[-1]['type'] == 'done'
    assert events[-1]['run_id'] == r1
    prompt = _prompt(fake_llm, 'interpret')
    assert 'FIRST_RUN_OUTPUT' in prompt and 'SECOND_RUN_OUTPUT' not in prompt
    ident = _identity(client)
    with app.app_context():
        assert 'significant' in storage.load_run(r1, identity=ident)[
            'interpretation']
        assert storage.load_run('r0002', identity=ident)['interpretation'] == ''


def test_interpret_defaults_to_latest_run(app, client, fake_llm):
    _seed(app, client, 'FIRST_RUN_OUTPUT')
    r2 = _seed(app, client, 'SECOND_RUN_OUTPUT')
    events = sse_events(post_json(client, '/interpret', {}))
    assert events[-1]['run_id'] == r2
    assert 'SECOND_RUN_OUTPUT' in _prompt(fake_llm, 'interpret')


def test_interpret_unknown_run_is_404(app, client, fake_llm):
    _seed(app, client, 'x')
    assert post_json(client, '/interpret',
                     {'run_id': 'r0404'}).status_code == 404
    assert post_json(client, '/interpret',
                     {'run_id': '../r0001'}).status_code == 404
    assert post_json(client, '/interpret', {'run_id': 7}).status_code == 400
    assert not fake_llm.calls


# ---------------------------------------------------------------------------
# /converse
# ---------------------------------------------------------------------------

def test_converse_selected_run_uses_its_interpretation(app, client, fake_llm):
    r1 = _seed(app, client, 'a', interp='FIRST_INTERP')
    _seed(app, client, 'b', interp='SECOND_INTERP')
    events = sse_events(post_json(client, '/converse',
                                  {'message': 'explain', 'run_id': r1}))
    assert events[-1]['run_id'] == r1
    prompt = _prompt(fake_llm, 'converse')
    assert 'FIRST_INTERP' in prompt and 'SECOND_INTERP' not in prompt


def test_converse_defaults_to_latest(app, client, fake_llm):
    _seed(app, client, 'a', interp='FIRST_INTERP')
    _seed(app, client, 'b', interp='SECOND_INTERP')
    sse_events(post_json(client, '/converse', {'message': 'explain'}))
    assert 'SECOND_INTERP' in _prompt(fake_llm, 'converse')


def test_converse_unknown_run_is_404(app, client):
    assert post_json(client, '/converse', {'message': 'x',
                                           'run_id': 'r0404'}).status_code == 404


# ---------------------------------------------------------------------------
# /generate_report
# ---------------------------------------------------------------------------

def test_report_grounds_on_each_selected_run(app, client, fake_llm):
    r1 = _seed(app, client, 'OUT_ONE', interp='INTERP_ONE')
    _seed(app, client, 'OUT_TWO', interp='INTERP_TWO')
    r3 = _seed(app, client, 'OUT_THREE', interp='INTERP_THREE')
    events = sse_events(post_json(client, '/generate_report', {
        'run_ids': [r3, r1], 'output': 'CLIENT_SPOOF'}))
    assert events[-1]['type'] == 'done'
    assert events[-1]['run_ids'] == [r1, r3]
    draft = _prompt(fake_llm, 'report_draft')
    for marker in ('OUT_ONE', 'INTERP_ONE', 'OUT_THREE', 'INTERP_THREE'):
        assert marker in draft
    assert 'OUT_TWO' not in draft and 'CLIENT_SPOOF' not in draft
    assert draft.index('OUT_ONE') < draft.index('OUT_THREE')


def test_report_defaults_to_latest_run(app, client, fake_llm):
    _seed(app, client, 'OUT_ONE', interp='INTERP_ONE')
    _seed(app, client, 'OUT_TWO', interp='INTERP_TWO')
    events = sse_events(post_json(client, '/generate_report', {}))
    assert events[-1]['run_ids'] == ['r0002']
    draft = _prompt(fake_llm, 'report_draft')
    assert 'OUT_TWO' in draft and 'OUT_ONE' not in draft


def test_report_multi_run_respects_prompt_budget(app, client, fake_llm):
    from statlee.routes import FREE_TEXT_MAX
    ids = [_seed(app, client, f'RUN{i}_' + 'x' * FREE_TEXT_MAX)
           for i in range(3)]
    sse_events(post_json(client, '/generate_report', {'run_ids': ids}))
    draft = _prompt(fake_llm, 'report_draft')
    for i in range(3):
        assert f'RUN{i}_' in draft
    assert draft.count('x') <= FREE_TEXT_MAX + 1000


def test_report_run_ids_validation(app, client, fake_llm):
    _seed(app, client, 'x')
    assert post_json(client, '/generate_report',
                     {'run_ids': ['r0404']}).status_code == 404
    assert post_json(client, '/generate_report',
                     {'run_ids': ['../x']}).status_code == 404
    assert post_json(client, '/generate_report',
                     {'run_ids': 'r0001'}).status_code == 400
    assert post_json(client, '/generate_report',
                     {'run_ids': [1]}).status_code == 400
    assert not [c for c in fake_llm.calls if c[1] == 'report_draft']
