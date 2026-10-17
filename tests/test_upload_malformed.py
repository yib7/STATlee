"""Malformed uploads get a clear, actionable JSON error (ship checklist 3.12).

Each bad file goes through the real /upload route and must come back as a
4xx JSON ``{"error": ...}`` that tells the user what to do, never a traceback
or a 200 with a nonsense dataset, and must leave nothing behind in the
workspace.
"""
import io
import os
import random

import pandas as pd
import pytest
from conftest import csrf_token


def _upload(client, raw, name):
    token = csrf_token(client)
    return client.post('/upload', data={'file': (io.BytesIO(raw), name)},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})


def _workspace_files(app, client):
    with client.session_transaction() as sess:
        identity = f"anon_{sess['sid']}"
    root = os.path.join(app.config['UPLOAD_FOLDER'], identity)
    if not os.path.isdir(root):
        return []
    return sorted(f for f in os.listdir(root) if not f.startswith('.'))


def _xlsx_bytes():
    buf = io.BytesIO()
    pd.DataFrame({'a': range(50), 'b': range(50)}).to_excel(buf, index=False)
    return buf.getvalue()


def _dta_bytes(tmp_path):
    path = tmp_path / 'x.dta'
    pd.DataFrame({'a': [1, 2], 'b': [3.0, 4.0]}).to_stata(path,
                                                          write_index=False)
    return path.read_bytes()


def _garbage(n=4096, seed=7):
    return random.Random(seed).randbytes(n)


# name -> (bytes factory, upload name, words the message must contain)
CASES = {
    'truncated xlsx': (lambda t: _xlsx_bytes()[:len(_xlsx_bytes()) // 2],
                       'data.xlsx', ('excel', 'damaged', 'csv')),
    'garbage named .xlsx': (lambda t: _garbage(), 'data.xlsx',
                            ('excel', 'damaged')),
    'garbage named .xls': (lambda t: _garbage(), 'data.xls',
                           ('excel', 'damaged')),
    'binary garbage named .csv': (lambda t: _garbage(), 'data.csv',
                                  ('not plain text', '.xlsx')),
    'binary with no NUL bytes named .csv': (
        lambda t: _garbage().replace(bytes([0]), bytes([1])), 'data.csv',
        ('not plain text',)),
    'NUL bytes named .csv': (lambda t: b'\x00' * 2048, 'data.csv',
                             ('not plain text',)),
    'xlsx renamed to .csv': (lambda t: _xlsx_bytes(), 'data.csv',
                             ('not plain text', '.xlsx')),
    'empty .csv': (lambda t: b'', 'data.csv', ('empty',)),
    'empty .tsv': (lambda t: b'', 'data.tsv', ('empty',)),
    'empty .xlsx': (lambda t: b'', 'data.xlsx', ('empty',)),
    'empty .dta': (lambda t: b'', 'data.dta', ('empty',)),
    'empty .sav': (lambda t: b'', 'data.sav', ('empty',)),
    'csv with only a header': (lambda t: b'a,b,c\n', 'data.csv',
                               ('no data rows',)),
    'tsv with only a header': (lambda t: b'a\tb\n', 'data.tsv',
                               ('no data rows',)),
    'blank lines only': (lambda t: b'\n\n\n', 'data.csv', ('empty',)),
    'truncated .dta': (lambda t: _dta_bytes(t)[:200], 'data.dta',
                       ('stata', 'damaged')),
    'garbage named .sav': (lambda t: _garbage(), 'data.sav',
                           ('spss', 'damaged')),
}


@pytest.mark.parametrize('case', sorted(CASES))
def test_malformed_upload_is_a_clear_json_error(app, client, tmp_path, case):
    make, name, words = CASES[case]
    resp = _upload(client, make(tmp_path), name)
    assert 400 <= resp.status_code < 500, (case, resp.status_code,
                                           resp.get_data(as_text=True)[:300])
    body = resp.get_json()
    assert isinstance(body, dict) and isinstance(body.get('error'), str), body
    message = body['error']
    assert 'Traceback' not in message and 'Error(' not in message
    for word in words:
        assert word.lower() in message.lower(), (case, message)
    assert _workspace_files(app, client) == []


def test_utf16_csv_with_a_bom_is_read_not_called_binary(client):
    raw = 'name,score\nJosé,1\nKöln,2\n'.encode('utf-16')
    resp = _upload(client, raw, 'wide.csv')
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['profile']['headers'] == ['name', 'score']
