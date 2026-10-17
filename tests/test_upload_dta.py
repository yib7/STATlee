"""Stata (.dta) uploads (ship checklist 3.13): the happy path converts to CSV
and seeds the codebook's native layer from the file's variable and value
labels; a damaged .dta is a clear 422 that leaves nothing behind."""
import io
import os

import pandas as pd
import pytest
from conftest import csrf_token, post_json

pyreadstat = pytest.importorskip('pyreadstat')


def _upload(client, raw, name):
    token = csrf_token(client)
    return client.post('/upload', data={'file': (io.BytesIO(raw), name)},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})


def _dta_bytes(tmp_path):
    path = tmp_path / 'survey.dta'
    df = pd.DataFrame({'sex': [1, 2, 2, 1],
                       'income': [1200.5, 2300.0, 1800.0, None],
                       'région': [1, 2, 3, 1]})
    pyreadstat.write_dta(
        df, str(path),
        column_labels={'sex': 'Respondent sex', 'income': 'Monthly income',
                       'région': 'Région de résidence'},
        variable_value_labels={'sex': {1: 'Male', 2: 'Female'},
                               'région': {1: 'Nord', 2: 'Sud', 3: 'Île'}})
    return path.read_bytes()


def test_dta_upload_converts_and_seeds_the_native_codebook(client, tmp_path):
    resp = _upload(client, _dta_bytes(tmp_path), 'survey.dta')
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['filename'] == 'survey.csv'
    assert body['profile']['headers'] == ['sex', 'income', 'région']
    assert body['labels']['sex'] == 'Respondent sex (1=Male, 2=Female)'

    book = client.get('/codebook',
                      query_string={'filename': 'survey.csv'}).get_json()
    sex = book['codebook']['sex']
    assert sex['source'] == 'native'
    assert sex['label'] == 'Respondent sex'
    assert sex['value_labels'] == {'1': 'Male', '2': 'Female'}
    region = book['codebook']['région']
    assert region['label'] == 'Région de résidence'
    assert region['value_labels'] == {'1': 'Nord', '2': 'Sud', '3': 'Île'}
    assert book['codebook']['income']['label'] == 'Monthly income'

    # The data itself reads back through the viewer (4 rows, one missing).
    rows = post_json(client, '/data_page',
                     {'filename': 'survey.csv'}).get_json()['data']
    assert len(rows) == 4
    assert rows[3]['income'] is None


def test_damaged_dta_is_a_clear_422_and_leaves_nothing(app, client, tmp_path):
    raw = _dta_bytes(tmp_path)
    resp = _upload(client, raw[:len(raw) // 3], 'survey.dta')
    assert resp.status_code == 422
    message = resp.get_json()['error']
    assert 'Stata' in message and 'damaged' in message
    with client.session_transaction() as sess:
        root = os.path.join(app.config['UPLOAD_FOLDER'], f"anon_{sess['sid']}")
    assert not os.path.isdir(root) or not [
        f for f in os.listdir(root) if not f.startswith('.')]
    # A failed upload does not replace a good one of the same name.
    assert _upload(client, raw, 'survey.dta').status_code == 200
    assert _upload(client, b'garbage' * 50, 'survey.dta').status_code == 422
    book = client.get('/codebook', query_string={'filename': 'survey.csv'})
    assert book.status_code == 200
    assert book.get_json()['codebook']['sex']['label'] == 'Respondent sex'
