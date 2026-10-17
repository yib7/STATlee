"""SP4: reproducibility bundle v2 (2026-10-08).

The export zip reproduces from the raw upload and documents what produced it:
README.md, data/original_<name>, the active dataset under the name the script
reads, cleaning.py, analysis.py|analysis.R, codebook.json, environment.txt,
provenance.json and outputs/run_NNNN/ per included run. Options:
``include_prompts`` (default true) and ``runs=all|starred|latest`` (default
all). No live LLM calls: every model reply comes from the fake service.
"""
import glob
import hashlib
import io
import json
import os
import zipfile

import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import environment, sandbox

PLOT_SCRIPT = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "import pandas as pd\n"
    "df = pd.read_csv('test.csv')\n"
    "print('ROWS', len(df))\n"
    "plt.plot([1, 2, 3])\n"
    "plt.savefig('plot_1.png')\n"
    "print('== METHOD ==')\n"
    "print('test: Welch t-test; outcome: income; predictors: group; "
    "n_used: 2; dropped: 0')\n"
    "print('== ASSUMPTIONS ==')\n"
    "print('Normality: W=0.97, p=0.62 -> PASS')\n"
    "print('== END ==')"
)  # no trailing newline: /chat approves the stripped script
QUESTION = 'Compare income across the PRIVATE_QUESTION groups'
WRANGLE_INSTRUCTION = 'drop SECRET_INSTRUCTION rows'


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _chat(client, fake_llm, code, prompt=QUESTION, filename='test.csv'):
    fake_llm.set('validation', code)
    sse_events(post_json(client, '/chat',
                         {'filename': filename, 'prompt': prompt}))


def _run(client, code, filename='test.csv'):
    resp = post_json(client, '/run', {'filename': filename, 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _generate_and_run(client, fake_llm, code, prompt=QUESTION,
                      filename='test.csv'):
    _chat(client, fake_llm, code, prompt, filename)
    return _run(client, code, filename)


def _wrangle(client, fake_llm, code, instruction, summary='Dropped rows'):
    fake_llm.set('wrangle', json.dumps({'code': code, 'summary': summary,
                                        'error': None}))
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': instruction})
    assert resp.status_code == 200, resp.get_json()


def _export(client, **extra):
    body = {'filename': 'test.csv', 'language': 'Python'}
    body.update(extra)
    resp = post_json(client, '/export', body)
    assert resp.status_code == 200, resp.data[:300]
    return zipfile.ZipFile(io.BytesIO(resp.data))


def _provenance(zf):
    return json.loads(zf.read('provenance.json').decode('utf-8'))


def _all_text(zf):
    return '\n'.join(zf.read(n).decode('utf-8', 'replace')
                     for n in zf.namelist() if not n.endswith('.png'))


@pytest.fixture
def full_session(client, fake_llm):
    """Upload, one cleaning step, one generated + run analysis, interpreted."""
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', WRANGLE_INSTRUCTION)
    run = _generate_and_run(client, fake_llm, PLOT_SCRIPT)
    assert run['success'], run['output']
    fake_llm.set('interpret', 'The groups differ.')
    sse_events(post_json(client, '/interpret', {'run_id': run['run_id']}))
    return run


# ---------------------------------------------------------------------------
# Plan checkpoint: unzip, run in the sandbox, every new file present
# ---------------------------------------------------------------------------

def test_bundle_runs_unzipped_in_the_sandbox_and_ships_every_file(
        client, full_session, tmp_path):
    zf = _export(client)
    names = set(zf.namelist())
    for name in ('README.md', 'analysis.py', 'test.csv',
                 'data/original_test.csv', 'cleaning.py', 'codebook.json',
                 'environment.txt', 'provenance.json', 'report.md',
                 'outputs/run_0001/output.txt',
                 'outputs/run_0001/plot_1.png',
                 'outputs/run_0001/methods.json',
                 'outputs/run_0001/interpretation.md'):
        assert name in names, (name, sorted(names))
    assert 'README.txt' not in names and 'script.py' not in names
    assert not any(n.startswith('plots/') for n in names)

    folder = tmp_path / 'bundle'
    zf.extractall(folder)
    code = (folder / 'analysis.py').read_text(encoding='utf-8')
    result = sandbox.run_in_sandbox(
        code, 'Python', dataset_path=str(folder / 'test.csv'),
        dataset_name='test.csv', timeout=120)
    assert result.success, result.output
    # The shipped data is the cleaned version (2 complete rows), as in-app.
    assert 'ROWS 2' in result.output
    assert 'ROWS 2' in zf.read('outputs/run_0001/output.txt').decode('utf-8')


def test_dataset_with_no_version_manifest_still_ships_its_data(
        app, client, full_session):
    """A legacy dataset has no readable ``.versions__<name>.json`` manifest, so
    there is no lineage; the export must still carry the dataset and its
    ``data/original_`` copy (and still return 200), and codebook.json (3A
    A2: the codebook falls back to the plain dataset file too)."""
    manifests = glob.glob(os.path.join(
        app.config['STATLEE'].upload_root, '**', '.versions__test.json'),
        recursive=True)
    assert len(manifests) == 1
    os.remove(manifests[0])
    zf = _export(client)
    names = set(zf.namelist())
    assert 'test.csv' in names
    assert 'data/original_test.csv' in names
    assert 'provenance.json' in names
    assert 'codebook.json' in names


def test_run_outputs_hold_methods_card_and_interpretation(client, full_session):
    zf = _export(client)
    card = json.loads(zf.read('outputs/run_0001/methods.json'))
    assert card['reported'] is True
    assert card['method']['test'] == 'Welch t-test'
    assert card['assumptions'][0]['status'] == 'pass'
    assert zf.read('outputs/run_0001/interpretation.md').decode() == \
        'The groups differ.'
    # The run's own script travels with its outputs.
    assert zf.read('outputs/run_0001/analysis.py').decode() == PLOT_SCRIPT
    assert zf.read('outputs/run_0001/plot_1.png')[:4] == b'\x89PNG'


def test_legacy_run_without_a_stored_card_still_gets_methods_json(
        app, client, fake_llm):
    """A run saved before SP3 (meta ``methods: null``) gets the same card
    GET /runs/<id> shows: parsed from its output."""
    upload_csv(client, SAMPLE_CSV)
    _generate_and_run(client, fake_llm, PLOT_SCRIPT)
    metas = glob.glob(os.path.join(app.config['STATLEE'].upload_root, '**',
                                   'runs', 'r0001', 'meta.json'),
                      recursive=True)
    assert len(metas) == 1
    with open(metas[0], encoding='utf-8') as f:
        meta = json.load(f)
    meta['methods'] = None
    with open(metas[0], 'w', encoding='utf-8') as f:
        json.dump(meta, f)
    card = json.loads(_export(client).read('outputs/run_0001/methods.json'))
    assert card['reported'] is True
    assert card['method']['test'] == 'Welch t-test'


# ---------------------------------------------------------------------------
# provenance.json
# ---------------------------------------------------------------------------

def test_provenance_shape_and_hashes(app, client, full_session):
    zf = _export(client)
    prov = _provenance(zf)
    assert set(prov) == {'generated_at', 'app_version', 'llm_provider', 'steps'}
    assert prov['llm_provider'] == app.config['STATLEE'].llm_provider
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        assert prov['app_version'] == tomllib.load(f)['project']['version']
    assert prov['generated_at'].endswith('+00:00')

    kinds = [s['kind'] for s in prov['steps']]
    assert kinds == ['upload', 'wrangle', 'run', 'interpret']
    allowed = {'kind', 'ts', 'prompt', 'prompt_inherited', 'fix_of', 'model', 'role',
               'dataset_sha256_before', 'dataset_sha256_after',
               'script_sha256', 'run_id'}
    for step in prov['steps']:
        assert set(step) <= allowed, step
        assert isinstance(step['ts'], str) and step['ts'].endswith('+00:00')
        assert all(v is not None for v in step.values()), step

    upload, wrangle, run, interp = prov['steps']
    original = _sha_bytes(zf.read('data/original_test.csv'))
    active = _sha_bytes(zf.read('test.csv'))
    assert upload['dataset_sha256_after'] == original
    assert 'prompt' not in upload
    assert wrangle['dataset_sha256_before'] == original
    assert wrangle['dataset_sha256_after'] == active
    assert wrangle['script_sha256'] == _sha_text('df = df.dropna()')
    assert wrangle['prompt'] == WRANGLE_INSTRUCTION
    assert wrangle['role'] == app.config['STATLEE'].wrangle_role
    assert wrangle['model'] == app.config['STATLEE'].model_flash_lite
    assert run['run_id'] == 'r0001'
    assert run['prompt'] == QUESTION
    assert run['prompt_inherited'] is False
    assert run['role'] == 'draft'
    assert run['model'] == app.config['STATLEE'].model_pro
    assert run['dataset_sha256_before'] == active
    assert run['script_sha256'] == _sha_text(PLOT_SCRIPT)
    assert run['script_sha256'] == _sha_bytes(zf.read('analysis.py'))
    assert interp == {'kind': 'interpret', 'ts': interp['ts'],
                      'run_id': 'r0001'}


def test_provenance_never_carries_data_values_or_system_prompts(
        client, full_session):
    text = _export(client).read('provenance.json').decode('utf-8')
    # Sample values from the CSV and system-prompt marker phrases.
    for needle in ('30000', '55000', '42000', 'expert social science',
                   'senior code reviewer', 'data wrangling engine'):
        assert needle not in text, needle


def test_include_prompts_false_strips_every_prompt(client, full_session):
    history = [{'role': 'user', 'text': QUESTION}]
    default = _export(client, history=history)
    assert QUESTION in default.read('provenance.json').decode()
    assert QUESTION in default.read('report.md').decode()

    zf = _export(client, include_prompts=False, history=history)
    prov = _provenance(zf)
    for step in prov['steps']:
        assert 'prompt' not in step and 'prompt_inherited' not in step
    blob = _all_text(zf)
    assert 'PRIVATE_QUESTION' not in blob
    assert 'SECRET_INSTRUCTION' not in blob
    assert 'left out' in zf.read('README.md').decode().lower()


def test_wrangle_records_role_and_model_on_the_version(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    steps = [s for s in _provenance(_export(client))['steps']
             if s['kind'] == 'wrangle']
    cfg = app.config['STATLEE']
    assert steps[0]['role'] == cfg.wrangle_role
    assert steps[0]['model'] == cfg.model_flash_lite


def test_provenance_follows_revert_lineage(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    assert post_json(client, '/revert_dataset',
                     {'filename': 'test.csv'}).status_code == 200
    _wrangle(client, fake_llm, "df['age2'] = df['age'] * 2", 'double age')
    zf = _export(client)
    steps = [s for s in _provenance(zf)['steps'] if s['kind'] == 'wrangle']
    # Only the step that produced the shipped data, starting from the original.
    assert len(steps) == 1
    assert steps[0]['dataset_sha256_before'] == \
        _sha_bytes(zf.read('data/original_test.csv'))
    assert steps[0]['dataset_sha256_after'] == _sha_bytes(zf.read('test.csv'))


# ---------------------------------------------------------------------------
# runs=all|starred|latest
# ---------------------------------------------------------------------------

def _three_runs(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    ids = []
    for n in range(3):
        code = f"print('RUN_{n}')\n"
        ids.append(_generate_and_run(client, fake_llm, code,
                                     prompt=f'question {n}')['run_id'])
    return ids


def _run_dirs(zf):
    return sorted({n.split('/')[1] for n in zf.namelist()
                   if n.startswith('outputs/')})


def _prov_runs(zf):
    return [s['run_id'] for s in _provenance(zf)['steps'] if s['kind'] == 'run']


def test_runs_option_selects_all_starred_or_latest(client, fake_llm):
    ids = _three_runs(client, fake_llm)
    resp = client.patch(f'/runs/{ids[0]}', json={'starred': True},
                        headers={'X-CSRF-Token': _token(client)})
    assert resp.status_code == 200

    every = _export(client)
    assert _run_dirs(every) == ['run_0001', 'run_0002', 'run_0003']
    assert _prov_runs(every) == ids
    assert _run_dirs(_export(client, runs='all')) == _run_dirs(every)

    starred = _export(client, runs='starred')
    assert _run_dirs(starred) == ['run_0001']
    assert _prov_runs(starred) == [ids[0]]

    latest = _export(client, runs='latest')
    assert _run_dirs(latest) == ['run_0003']
    assert b'RUN_2' in latest.read('outputs/run_0003/output.txt')


def test_starred_with_no_starred_runs_ships_no_run_outputs(client, fake_llm):
    _three_runs(client, fake_llm)
    zf = _export(client, runs='starred')
    assert _run_dirs(zf) == []
    assert 'no runs' in zf.read('README.md').decode().lower()


def _token(client):
    from conftest import csrf_token
    return csrf_token(client)


@pytest.mark.parametrize('body', [
    {'runs': 'everything'}, {'runs': 3}, {'runs': None},
    {'include_prompts': 'false'}, {'include_prompts': 0},
])
def test_export_options_are_validated(client, body):
    upload_csv(client, SAMPLE_CSV)
    payload = {'filename': 'test.csv', 'language': 'Python'}
    payload.update(body)
    resp = post_json(client, '/export', payload)
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_export_caps_runs_at_the_configured_maximum(app, client, fake_llm):
    ids = _three_runs(client, fake_llm)
    app.config['STATLEE'].max_runs_per_identity = 2
    zf = _export(client)
    assert _run_dirs(zf) == ['run_0002', 'run_0003']
    assert _prov_runs(zf) == ids[1:]


def test_a_run_that_vanishes_mid_export_is_skipped(client, fake_llm,
                                                   monkeypatch):
    from statlee import storage
    ids = _three_runs(client, fake_llm)
    real = storage.load_run

    def flaky(run_id, identity=None):
        return None if run_id == ids[1] else real(run_id, identity)
    monkeypatch.setattr(storage, 'load_run', flaky)
    zf = _export(client)
    assert _run_dirs(zf) == ['run_0001', 'run_0003']
    assert _prov_runs(zf) == [ids[0], ids[2]]


def test_a_plot_outside_the_run_dir_is_never_bundled(client, fake_llm,
                                                     monkeypatch, tmp_path):
    from statlee import storage
    ids = _three_runs(client, fake_llm)
    outside = tmp_path / 'outside_secret.png'
    outside.write_bytes(b'\x89PNG secret')
    real = storage.load_run

    def poisoned(run_id, identity=None):
        record = real(run_id, identity)
        if record and run_id == ids[0]:
            record['plots'] = record['plots'] + [str(outside)]
        return record
    monkeypatch.setattr(storage, 'load_run', poisoned)
    zf = _export(client)
    assert 'outputs/run_0001/output.txt' in zf.namelist()
    assert not any(b'secret' in zf.read(n) for n in zf.namelist()
                   if n.endswith('.png'))


# ---------------------------------------------------------------------------
# codebook.json, environment.txt, README.md
# ---------------------------------------------------------------------------

def test_codebook_json_round_trips_through_import(client, full_session):
    headers = {'X-CSRF-Token': _token(client)}
    resp = client.put('/codebook', json={
        'filename': 'test.csv',
        'codebook': {'income': {'label': 'Annual income (USD)',
                                'missing_codes': ['-9']}}}, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    doc = json.loads(_export(client).read('codebook.json'))
    assert doc['format'] == 'statlee-codebook'
    assert doc['codebook']['income']['label'] == 'Annual income (USD)'

    # Change it, then import the bundled document: the bundle's values return.
    client.put('/codebook', json={'filename': 'test.csv', 'codebook': {
        'income': {'label': 'changed'}}}, headers=headers)
    resp = client.post('/codebook/import', json={'filename': 'test.csv',
                                                  'codebook': doc},
                       headers=headers)
    assert resp.status_code == 200, resp.get_json()
    entry = resp.get_json()['codebook']['income']
    assert entry['label'] == 'Annual income (USD)'
    assert entry['missing_codes'] == ['-9']


def test_environment_txt_lists_python_and_packages(client, full_session):
    text = _export(client).read('environment.txt').decode('utf-8')
    assert 'Python 3.' in text
    import numpy
    import pandas
    assert f'pandas {pandas.__version__}' in text
    assert f'numpy {numpy.__version__}' in text
    assert 'could not be captured' not in text


def test_probe_failure_still_exports_and_says_so(client, full_session,
                                                 monkeypatch):
    environment.clear_cache()

    class Failed:
        success = False
        timed_out = True
        returncode = -1
        output = 'Traceback: C:\\server\\path secret'
        plots = []
        files = {}
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox',
                        lambda *a, **k: Failed())
    try:
        zf = _export(client)
        text = zf.read('environment.txt').decode('utf-8')
        assert 'could not be captured' in text
        assert 'secret' not in text
        assert 'analysis.py' in zf.namelist()
    finally:
        environment.clear_cache()


def test_readme_md_explains_how_to_run_and_how_it_was_produced(
        app, client, full_session):
    readme = _export(client).read('README.md').decode('utf-8')
    assert readme.startswith('# ')
    assert 'python analysis.py' in readme
    assert 'python cleaning.py' in readme
    for name in ('test.csv', 'data/original_test.csv', 'codebook.json',
                 'environment.txt', 'provenance.json', 'outputs/run_0001/'):
        assert name in readme, name
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        version = tomllib.load(f)['project']['version']
    assert f'STATlee {version}' in readme
    assert app.config['STATLEE'].llm_provider in readme
    assert '\u2014' not in readme and '\u2013' not in readme


def test_r_script_is_bundled_as_analysis_r(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    code = 'df <- read.csv("test.csv")\ncat("ROWS", nrow(df), "\\n")\n'
    fake_llm.set('validation', code)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'rows',
                                           'language': 'R'}))
    zf = _export(client, language='R', code=code)
    assert 'analysis.R' in zf.namelist()
    assert 'Rscript analysis.R' in zf.read('README.md').decode()


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------

def test_only_runs_on_the_exported_dataset_are_bundled(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    upload_csv(client, SAMPLE_CSV.replace('income', 'salary'), 'other.csv')
    on_test = _generate_and_run(client, fake_llm, "print('ON_TEST')",
                                prompt='test question')['run_id']
    on_other = _generate_and_run(client, fake_llm, "print('ON_OTHER')",
                                 prompt='other question',
                                 filename='other.csv')['run_id']
    zf = _export(client)
    assert _prov_runs(zf) == [on_test]
    assert _run_dirs(zf) == ['run_' + on_test[1:]]
    assert 'other question' not in _all_text(zf)
    assert b'ON_TEST' in zf.read('analysis.py')
    other = _export(client, filename='other.csv')
    assert _prov_runs(other) == [on_other]
    assert b'ON_OTHER' in other.read('analysis.py')


def test_analysis_script_is_the_latest_run_in_the_selected_set(
        client, fake_llm):
    ids = _three_runs(client, fake_llm)
    client.patch(f'/runs/{ids[0]}', json={'starred': True},
                 headers={'X-CSRF-Token': _token(client)})
    starred = _export(client, runs='starred')
    assert starred.read('analysis.py').strip() == b"print('RUN_0')"
    run_step = [s for s in _provenance(starred)['steps']
                if s['kind'] == 'run'][-1]
    assert run_step['script_sha256'] == _sha_bytes(starred.read('analysis.py'))
    readme = starred.read('README.md').decode()
    assert ids[0] in readme and 'outputs/run_0001/' in readme
    # report.md describes the same run.
    report = starred.read('report.md').decode()
    assert 'RUN_0' in report and 'RUN_2' not in report

    every = _export(client)
    assert every.read('analysis.py').strip() == b"print('RUN_2')"
    assert ids[2] in every.read('README.md').decode()


def test_with_no_selected_runs_the_latest_approved_script_is_bundled(
        client, fake_llm):
    _three_runs(client, fake_llm)
    _chat(client, fake_llm, "print('GENERATED_NOT_RUN')")
    zf = _export(client, runs='starred')            # nothing starred
    assert zf.read('analysis.py') == b"print('GENERATED_NOT_RUN')"


def test_plots_are_stored_uncompressed(client, full_session):
    zf = _export(client)
    info = zf.getinfo('outputs/run_0001/plot_1.png')
    assert info.compress_type == zipfile.ZIP_STORED
    assert zf.getinfo('analysis.py').compress_type == zipfile.ZIP_DEFLATED


def test_zip_is_spooled_to_a_temp_file_and_closed_after_the_response(
        client, full_session, monkeypatch):
    import tempfile

    from statlee.routes import export
    made = []
    real = tempfile.SpooledTemporaryFile

    def spy(*args, **kwargs):
        spool = real(*args, **kwargs)
        made.append((kwargs.get('max_size'), spool))
        return spool
    monkeypatch.setattr(export.tempfile, 'SpooledTemporaryFile', spy)
    resp = post_json(client, '/export', {'filename': 'test.csv'})
    assert resp.status_code == 200
    assert int(resp.headers['Content-Length']) == len(resp.data)
    assert zipfile.ZipFile(io.BytesIO(resp.data)).testzip() is None
    resp.close()
    assert len(made) == 1
    assert made[0][0] and made[0][0] > 0
    assert made[0][1].closed


def test_readme_does_not_list_a_dataset_file_that_was_skipped(
        client, full_session, monkeypatch):
    from statlee import bundle
    real = bundle.write_file

    def skip_data(zf, path, arcname, root):
        if arcname in ('test.csv', 'data/original_test.csv'):
            return False
        return real(zf, path, arcname, root)
    monkeypatch.setattr(bundle, 'write_file', skip_data)
    zf = _export(client)
    assert 'test.csv' not in zf.namelist()
    readme = zf.read('README.md').decode()
    assert '- test.csv:' not in readme
    assert 'data/original_test.csv' not in readme
    assert 'cleaning.py' not in zf.namelist()


def test_readme_report_line_does_not_promise_requests_without_prompts(
        client, full_session):
    with_prompts = _export(client).read('README.md').decode()
    without = _export(client, include_prompts=False).read('README.md').decode()
    line = [x for x in with_prompts.splitlines() if x.startswith('- report.md')]
    assert 'request' in line[0]
    line = [x for x in without.splitlines() if x.startswith('- report.md')]
    assert 'request' not in line[0]
    assert '—' not in without


def test_export_is_rate_limited(config, reset_limiter_state, fake_llm):
    from statlee import llm
    from statlee.app import create_app
    config.rate_limit_enabled = True
    config.rate_limit_run = '2 per minute'
    application = create_app(config)
    llm.set_service(fake_llm)
    c = application.test_client()
    codes = [post_json(c, '/export', {'code': "print('x')"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


# ---------------------------------------------------------------------------
# Final review fixes
# ---------------------------------------------------------------------------

def test_fallback_never_ships_another_datasets_script_output_or_interpretation(
        client, fake_llm):
    """No run on test.csv, a run on other.csv: the test.csv bundle must not
    carry other.csv's script, output, or the client-sent interpretation."""
    upload_csv(client, SAMPLE_CSV)
    upload_csv(client, SAMPLE_CSV.replace('income', 'salary'), 'other.csv')
    other_code = "print('ON_OTHER_ONLY')"
    _generate_and_run(client, fake_llm, other_code, prompt='other question',
                      filename='other.csv')
    zf = _export(client, code=other_code,
                 interpretation='CLIENT_SENT_INTERPRETATION')
    names = zf.namelist()
    assert 'analysis.py' not in names and 'analysis.R' not in names
    text = _all_text(zf)
    assert 'ON_OTHER_ONLY' not in text
    assert 'CLIENT_SENT_INTERPRETATION' not in text
    readme = zf.read('README.md').decode()
    assert 'The script reads test.csv' not in readme
    assert 'no analysis has been run on this dataset yet' in readme.lower()
    # other.csv's own export still has its script.
    other = _export(client, filename='other.csv')
    assert b'ON_OTHER_ONLY' in other.read('analysis.py')


def test_fallback_uses_a_script_generated_for_this_dataset(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    upload_csv(client, SAMPLE_CSV.replace('income', 'salary'), 'other.csv')
    _chat(client, fake_llm, "print('FOR_TEST')", prompt='q', filename='test.csv')
    _generate_and_run(client, fake_llm, "print('FOR_OTHER')", prompt='q2',
                      filename='other.csv')
    zf = _export(client, interpretation='CLIENT_SENT_INTERPRETATION')
    assert zf.read('analysis.py') == b"print('FOR_TEST')"
    assert 'CLIENT_SENT_INTERPRETATION' not in _all_text(zf)
    assert 'FOR_OTHER' not in _all_text(zf)


def test_runs_on_earlier_content_of_a_same_name_dataset_are_left_out(
        client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    old = _generate_and_run(client, fake_llm, "print('OLD_CONTENT')",
                            prompt='old q')['run_id']
    upload_csv(client, SAMPLE_CSV.replace('30000', '31000'))
    new = _generate_and_run(client, fake_llm, "print('NEW_CONTENT')",
                            prompt='new q')['run_id']
    zf = _export(client)
    assert _prov_runs(zf) == [new]
    assert _run_dirs(zf) == ['run_' + new[1:]]
    assert old not in _prov_runs(zf)
    assert 'OLD_CONTENT' not in _all_text(zf)
    readme = zf.read('README.md').decode()
    assert ('1 run was left out because it read a dataset version that is '
            'not in this bundle.') in readme
    assert 'earlier content' not in readme


def test_bundle_readme_mentions_pre_1_6_0_runs_only_when_one_was_left_out(
        app, client, full_session):
    """3A A8: the pre-1.6.0 line used to print on every export."""
    readme = _export(client).read('README.md').decode()
    assert 'before STATlee 1.6.0' not in readme
    assert 'all runs on this dataset' in readme
    with client.session_transaction() as sess:
        ident = f"anon_{sess['sid']}"
    with app.app_context():
        from statlee import storage
        legacy = os.path.join(storage.identity_root(ident),
                              storage.LAST_RUN_DIR)
    os.makedirs(legacy)
    with open(os.path.join(legacy, 'output.txt'), 'w', encoding='utf-8') as f:
        f.write('LEGACY_RUN_OUTPUT')
    zf = _export(client)
    readme = zf.read('README.md').decode()
    assert ('1 run is not included because it did not record which dataset '
            'it read (saved before STATlee 1.6.0') in readme
    assert 'LEGACY_RUN_OUTPUT' not in _all_text(zf)
    # An export with no dataset ships the legacy run and says nothing.
    resp = post_json(client, '/export', {'language': 'Python'})
    nodata = zipfile.ZipFile(io.BytesIO(resp.data))
    assert 'before STATlee 1.6.0' not in nodata.read('README.md').decode()
    assert 'LEGACY_RUN_OUTPUT' in _all_text(nodata)


def test_readme_left_out_counts_only_runs_the_selection_would_ship(
        client, fake_llm):
    """C-22: a left-out run that "latest" or "starred" would never have
    shipped is not reported."""
    upload_csv(client, SAMPLE_CSV)
    old = _generate_and_run(client, fake_llm, "print('OLD')",
                            prompt='old q')['run_id']
    upload_csv(client, SAMPLE_CSV.replace('30000', '31000'))
    _generate_and_run(client, fake_llm, "print('NEW')", prompt='new q')
    left_line = 'left out because'
    assert left_line in _export(client, runs='all').read(
        'README.md').decode()
    assert left_line not in _export(client, runs='latest').read(
        'README.md').decode()
    assert left_line not in _export(client, runs='starred').read(
        'README.md').decode()
    resp = client.patch(f'/runs/{old}', json={'starred': True},
                        headers={'X-CSRF-Token': _token(client)})
    assert resp.status_code == 200
    starred = _export(client, runs='starred')
    assert _run_dirs(starred) == []
    assert '1 run was left out because it read a dataset version' in \
        starred.read('README.md').decode()


def test_readme_counts_a_newest_left_out_run_under_latest(client, fake_llm):
    """The newest run on this name read a version that is not in the bundle
    (a redo branch undone, here an older upload restored): "latest" would
    have shipped it, so README.md says it was left out."""
    upload_csv(client, SAMPLE_CSV)
    _generate_and_run(client, fake_llm, "print('FIRST')", prompt='q1')
    upload_csv(client, SAMPLE_CSV.replace('30000', '31000'))
    _generate_and_run(client, fake_llm, "print('SECOND')", prompt='q2')
    upload_csv(client, SAMPLE_CSV)        # back to the first content
    zf = _export(client, runs='latest')
    assert 'FIRST' in zf.read('analysis.py').decode()
    assert '1 run was left out because it read a dataset version' in \
        zf.read('README.md').decode()
