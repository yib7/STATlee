"""Reserved version-file names are refused on every dataset route (L8).

Version files are ``stem__vN.csv`` in the same flat workspace. Uploads already
refused such names (P2-3), but a client could still pass ``test__v2.csv`` to
/wrangle and the rest: the route read the version file as if it were a dataset
and grew a second history (``test__v2__v2.csv``) on top of it.
"""
import json

import pytest
from conftest import SAMPLE_CSV, post_json, upload_csv


def _plan(fake_llm, code, summary):
    fake_llm.set('wrangle', json.dumps({'code': code, 'summary': summary,
                                        'error': None}))


@pytest.mark.parametrize('path, extra', [
    ('/wrangle', {'instruction': 'drop missing rows'}),
    ('/data_page', {}),
    ('/dataset_versions', {}),
    ('/version_control', {'direction': 'undo'}),
    ('/revert_dataset', {}),
    ('/classify_variables', {}),
    ('/suggest', {}),
    ('/export', {'language': 'Python'}),
    ('/run', {'code': "print('hi')", 'language': 'Python'}),
    ('/chat', {'prompt': 'summarize'}),
    ('/method_prompt', {'method': {'name': 'T-test'}}),
])
def test_reserved_version_file_names_are_refused(client, fake_llm, path, extra):
    upload_csv(client, SAMPLE_CSV)
    _plan(fake_llm, code='df = df.dropna()', summary='drop missing')
    assert post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop missing'}
                     ).status_code == 200
    calls_before = len(fake_llm.calls)
    body = {'filename': 'test__v2.csv'}
    body.update(extra)
    resp = post_json(client, path, body)
    assert resp.status_code == 400, (path, resp.status_code, resp.get_data())
    assert 'reserved' in resp.get_json()['error'].lower()
    assert len(fake_llm.calls) == calls_before        # refused before any call
