"""A wrangle commits only on top of the exact version it read (S5).

L1 added a 409 when the active version moved under a running /wrangle, but the
check compared version FILE NAMES. Two cases slipped through:

* a re-upload under the same name resets the manifest to v1 with the same file
  name, so a wrangle that read the OLD upload saved a v2 built from the old
  data on top of the new upload;
* undo followed by a new wrangle rebuilds ``stem__v2.csv`` with different
  content, so a wrangle that read the first v2 matched by name.

The manifest now carries a version token (a per-upload id plus a revision
counter bumped by every change), and /wrangle compares that.
"""
import json

import pandas as pd
import pytest
from conftest import SAMPLE_CSV, post_json, upload_csv

from statlee import storage
from statlee.routes import datasets as datasets_mod

NEW_CSV = "x,y\n1,2\n3,4\n5,6\n7,8\n9,10\n"


def _plan(fake_llm, code='df = df.head(1)', summary='first row'):
    fake_llm.set('wrangle', json.dumps({'code': code, 'summary': summary,
                                        'error': None}))


def _rows(client, name='test.csv'):
    resp = post_json(client, '/data_page', {'filename': name})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['data']


def test_reupload_during_a_wrangle_is_a_409_not_a_stale_v2(
        client, fake_llm, monkeypatch):
    upload_csv(client, SAMPLE_CSV)
    _plan(fake_llm)
    real_run = datasets_mod.sandbox.run_in_sandbox

    def run_then_reupload(*args, **kwargs):
        result = real_run(*args, **kwargs)
        # What /upload does for a same-name re-upload, landing while the
        # wrangle's transform is still running: replace the file, reset the
        # history to v1.
        target = storage.resolve_path('test.csv')
        with open(target, 'w', encoding='utf-8', newline='') as f:
            f.write(NEW_CSV)
        storage.register_dataset('test.csv')
        return result

    monkeypatch.setattr(datasets_mod.sandbox, 'run_in_sandbox',
                        run_then_reupload)
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'first row only'})
    assert resp.status_code == 409, resp.get_json()
    monkeypatch.setattr(datasets_mod.sandbox, 'run_in_sandbox', real_run)
    # The new upload is untouched: still v1, still its own five rows.
    log = post_json(client, '/dataset_versions', {'filename': 'test.csv'})
    assert log.get_json()['changelog']['active'] == 1
    rows = _rows(client)
    assert len(rows) == 5 and set(rows[0]) == {'x', 'y'}


def test_undo_then_rewrangle_invalidates_an_older_base(app):
    """Same version number and file name, different content: the token must
    still differ."""
    with app.test_request_context():
        ident = 'anon_s5token'
        root = storage.identity_root(ident)
        pd.DataFrame({'a': [1, 2, 3]}).to_csv(f'{root}/d.csv', index=False)
        storage.register_dataset('d.csv', ident)
        storage.add_dataset_version('d.csv', pd.DataFrame({'a': [1]}), 'one',
                                    code='df = df.head(1)', identity=ident)
        stale = storage.dataset_version_token('d.csv', ident)
        storage.shift_dataset_version('d.csv', 'undo', ident)
        storage.add_dataset_version('d.csv', pd.DataFrame({'a': [3]}), 'three',
                                    code='df = df.tail(1)', identity=ident)
        assert storage.dataset_changelog('d.csv', ident)['active'] == 2
        with pytest.raises(storage.VersionConflict):
            storage.add_dataset_version(
                'd.csv', pd.DataFrame({'a': [9]}), 'late', code='df = df',
                base_token=stale, identity=ident)


def test_reupload_changes_the_token_even_with_the_same_name(app):
    with app.test_request_context():
        ident = 'anon_s5upload'
        root = storage.identity_root(ident)
        pd.DataFrame({'a': [1]}).to_csv(f'{root}/d.csv', index=False)
        storage.register_dataset('d.csv', ident)
        first = storage.dataset_version_token('d.csv', ident)
        storage.register_dataset('d.csv', ident)
        assert storage.dataset_version_token('d.csv', ident) != first


def test_a_wrangle_on_an_unchanged_dataset_still_commits(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _plan(fake_llm)
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'first row only'})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['changelog']['active'] == 2
