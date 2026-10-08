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


def _chat(client, fake_llm, code, prompt=QUESTION):
    fake_llm.set('validation', code)
    sse_events(post_json(client, '/chat',
                         {'filename': 'test.csv', 'prompt': prompt}))


def _run(client, code):
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _generate_and_run(client, fake_llm, code, prompt=QUESTION):
    _chat(client, fake_llm, code, prompt)
    return _run(client, code)


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
    allowed = {'kind', 'ts', 'prompt', 'prompt_inherited', 'model', 'role',
               'dataset_sha256_before', 'dataset_sha256_after',
               'dataset_sha256', 'script_sha256', 'run_id'}
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
    assert run['dataset_sha256'] == active
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
