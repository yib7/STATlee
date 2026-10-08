"""The exported project runs as unzipped (deep audit D4-9 + D6).

* analysis.py reads the dataset under the name the sandbox staged it as, so the
  active version must sit next to it at the zip root (it used to be
  ``data/<name>__vN.csv``: unzip, run, FileNotFoundError).
* The script is the last generated or run ANALYSIS script, never a /wrangle
  transform, and never unreviewed editor text when a reviewed one exists.
* The original upload ships as ``data/original_<name>``, with README.md.
* cleaning.py replays the recorded cleaning steps from the original (D6).
* A wrangle whose base version moved underneath it is refused (L1).
"""
import io
import json
import subprocess
import sys
import zipfile

import pandas as pd
import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import storage

READS_DATA = ("import pandas as pd\n"
              "df = pd.read_csv('test.csv')\n"
              "print('ROWS', len(df))\n")


def _chat(client, fake_llm, code):
    fake_llm.set('validation', code)
    sse_events(post_json(client, '/chat',
                         {'filename': 'test.csv', 'prompt': 'summarize'}))


def _run(client, code):
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _wrangle(client, fake_llm, code, summary='step'):
    fake_llm.set('wrangle', json.dumps({'code': code, 'summary': summary,
                                        'error': None}))
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': summary})
    assert resp.status_code == 200, resp.get_json()


def _export(client, **extra):
    body = {'filename': 'test.csv', 'language': 'Python'}
    body.update(extra)
    resp = post_json(client, '/export', body)
    assert resp.status_code == 200
    return zipfile.ZipFile(io.BytesIO(resp.data))


def _unzip(zf, tmp_path):
    out = tmp_path / 'unzipped'
    zf.extractall(out)
    return out


def _py(folder, script):
    return subprocess.run([sys.executable, script], cwd=folder,
                          capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# D4-9: runs as unzipped
# ---------------------------------------------------------------------------

def test_exported_script_runs_as_unzipped(client, fake_llm, tmp_path):
    upload_csv(client, SAMPLE_CSV)
    _chat(client, fake_llm, READS_DATA)
    assert 'ROWS 4' in _run(client, READS_DATA)['output']
    folder = _unzip(_export(client), tmp_path)
    proc = _py(folder, 'analysis.py')
    assert proc.returncode == 0, proc.stderr
    assert 'ROWS 4' in proc.stdout


def test_exported_data_is_the_active_version_under_the_staged_name(
        client, fake_llm, tmp_path):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    _chat(client, fake_llm, READS_DATA)
    sandbox_out = _run(client, READS_DATA)['output']
    zf = _export(client)
    names = zf.namelist()
    assert 'test.csv' in names
    assert not any('__v' in n for n in names)
    folder = _unzip(zf, tmp_path)
    proc = _py(folder, 'analysis.py')
    assert proc.returncode == 0, proc.stderr
    # Same answer as the in-app run (2 complete rows), not the raw upload's 4.
    assert 'ROWS 2' in sandbox_out and 'ROWS 2' in proc.stdout


def test_export_ships_original_upload_and_readme(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    zf = _export(client)
    original = pd.read_csv(io.BytesIO(zf.read('data/original_test.csv')))
    assert len(original) == 4
    readme = zf.read('README.md').decode('utf-8')
    assert 'test.csv' in readme
    assert 'original_test.csv' in readme


# ---------------------------------------------------------------------------
# D4-9: which script gets bundled
# ---------------------------------------------------------------------------

def test_export_bundles_newer_generated_script_over_older_run(
        client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _chat(client, fake_llm, "print('FIRST')")
    _run(client, "print('FIRST')")
    _chat(client, fake_llm, "print('SECOND')")       # generated, not yet run
    script = _export(client).read('analysis.py').decode('utf-8')
    assert 'SECOND' in script


def test_export_prefers_reviewed_editor_code(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _chat(client, fake_llm, "print('FIRST')")
    _chat(client, fake_llm, "print('SECOND')")
    # The editor shows the FIRST script (reviewed earlier in the session).
    script = _export(client, code="print('FIRST')").read('analysis.py')
    assert b'FIRST' in script


def test_export_does_not_bundle_unreviewed_editor_code_over_reviewed(
        client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _chat(client, fake_llm, "print('REVIEWED')")
    zf = _export(client, code="print('UNREVIEWED EDIT')")
    assert b'REVIEWED' in zf.read('analysis.py')
    assert b'UNREVIEWED' not in zf.read('analysis.py')
    assert 'editor' in zf.read('README.md').decode('utf-8').lower()


def test_export_never_bundles_a_wrangle_transform_as_the_script(
        client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _chat(client, fake_llm, "print('ANALYSIS')")
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    zf = _export(client, code='df = df.dropna()')
    assert b'ANALYSIS' in zf.read('analysis.py')


# ---------------------------------------------------------------------------
# D6: cleaning.py replays the recorded steps
# ---------------------------------------------------------------------------

def test_wrangle_records_step_code_in_history(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    zf = _export(client)
    assert 'cleaning.py' in zf.namelist()
    assert 'df = df.dropna()' in zf.read('cleaning.py').decode('utf-8')


def _replay_matches(zf, tmp_path):
    folder = _unzip(zf, tmp_path)
    shipped = pd.read_csv(folder / 'test.csv')
    (folder / 'test.csv').unlink()
    proc = _py(folder, 'cleaning.py')
    assert proc.returncode == 0, proc.stderr
    rebuilt = pd.read_csv(folder / 'test.csv')
    pd.testing.assert_frame_equal(rebuilt, shipped)
    return rebuilt


def test_cleaning_py_rebuilds_the_active_version(client, fake_llm, tmp_path):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    _wrangle(client, fake_llm, "df['income_k'] = df['income'] / 1000",
             'income in thousands')
    rebuilt = _replay_matches(_export(client), tmp_path)
    assert list(rebuilt.columns)[-1] == 'income_k' and len(rebuilt) == 2


def test_cleaning_py_follows_revert_and_undo(client, fake_llm, tmp_path):
    upload_csv(client, SAMPLE_CSV)
    _wrangle(client, fake_llm, 'df = df.dropna()', 'drop missing')
    assert post_json(client, '/revert_dataset',
                     {'filename': 'test.csv'}).status_code == 200
    _wrangle(client, fake_llm, "df['age2'] = df['age'] * 2", 'double age')
    _wrangle(client, fake_llm, 'df = df.head(1)', 'first row only')
    post_json(client, '/version_control',
              {'filename': 'test.csv', 'direction': 'undo'})
    rebuilt = _replay_matches(_export(client), tmp_path)
    # Revert restarted from the original (4 rows kept), the undone step is gone.
    assert len(rebuilt) == 4 and 'age2' in rebuilt.columns


def test_no_cleaning_py_without_cleaning_steps(client):
    upload_csv(client, SAMPLE_CSV)
    assert 'cleaning.py' not in _export(client).namelist()


# ---------------------------------------------------------------------------
# L1: a wrangle whose base version moved is refused, not silently stacked
# ---------------------------------------------------------------------------

def test_add_version_refuses_a_stale_base(app):
    """Two wrangles both read v1; the first commits v2. The second must not
    stack its v1-derived frame on top of v2 as if it had seen v2's changes."""
    with app.test_request_context():
        ident = 'anon_l1test'
        root = storage.identity_root(ident)
        pd.DataFrame({'a': [1, 2]}).to_csv(f'{root}/d.csv', index=False)
        storage.register_dataset('d.csv', ident)
        storage.add_dataset_version('d.csv', pd.DataFrame({'a': [1]}), 'first',
                                    code='df = df.head(1)', base_file='d.csv',
                                    identity=ident)
        with pytest.raises(storage.VersionConflict):
            storage.add_dataset_version(
                'd.csv', pd.DataFrame({'a': [2]}), 'second', code='df = df',
                base_file='d.csv', identity=ident)
        assert storage.dataset_changelog('d.csv', ident)['active'] == 2
