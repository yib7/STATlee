"""Upload robustness (deep audit D1-1, D1-8, D1-10, D1-13, D1-14).

* Non-Latin filenames keep their extension, get a safe distinct stored name,
  and still pass through the PDF page cap.
* cp1252 CSVs decode, empty / malformed CSVs are a clear 422, and no failure
  path leaves an unregistered file counting toward the quota.
* Re-uploading the same name at the file-count quota replaces the file.
* TXT codebooks keep non-Latin text.
* SPSS value labels and multi-sheet workbooks are not silently dropped.
"""
import io
import os

import pandas as pd
import pytest
from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv


def _upload_bytes(client, raw, filename, route='/upload'):
    token = csrf_token(client)
    return client.post(route, data={'file': (io.BytesIO(raw), filename)},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})


def _identity_files(app, client):
    csrf_token(client)
    with client.session_transaction() as sess:
        identity = f"anon_{sess['sid']}"
    root = os.path.join(app.config['UPLOAD_FOLDER'], identity)
    if not os.path.isdir(root):
        return []
    return sorted(f for f in os.listdir(root) if not f.startswith('.'))


def _pdf_bytes(pages):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# D1-1 non-Latin filenames
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name', ['数据.csv', 'данные.csv', 'بيانات.csv'])
def test_non_latin_csv_name_uploads(client, name):
    resp = _upload_bytes(client, SAMPLE_CSV.encode(), name)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['filename'].endswith('.csv')
    assert body['filename'] != '.csv'
    assert body['original_name'] == name
    page = post_json(client, '/data_page', {'filename': body['filename']})
    assert page.status_code == 200
    assert page.get_json()['total_rows'] == 4


def test_distinct_non_latin_names_do_not_collide(client):
    a = _upload_bytes(client, b'x\n1\n', '调查 2024.csv').get_json()['filename']
    b = _upload_bytes(client, b'y\n2\n', '数据 2024.csv').get_json()['filename']
    assert a != b
    assert post_json(client, '/data_page', {'filename': a}).get_json()['data'] == [{'x': 1}]
    assert post_json(client, '/data_page', {'filename': b}).get_json()['data'] == [{'y': 2}]


def test_same_non_latin_name_is_stable(client):
    a = _upload_bytes(client, b'x\n1\n', '数据.csv').get_json()['filename']
    b = _upload_bytes(client, b'x\n2\n', '数据.csv').get_json()['filename']
    assert a == b


def test_latin_accents_still_transliterate(client):
    resp = _upload_bytes(client, SAMPLE_CSV.encode(), 'café.csv')
    assert resp.get_json()['filename'] == 'cafe.csv'


def test_non_latin_pdf_respects_page_cap(client, app):
    app.config['STATLEE'].pdf_max_pages = 50
    resp = _upload_bytes(client, _pdf_bytes(80), 'коды.pdf', '/upload_pdf')
    assert resp.status_code == 413, resp.get_json()
    assert '80 pages' in resp.get_json()['error']


def test_non_latin_pdf_within_cap_keeps_extension(client, app):
    resp = _upload_bytes(client, _pdf_bytes(2), 'коды.pdf', '/upload_pdf')
    assert resp.status_code == 200
    assert resp.get_json()['filename'].endswith('.pdf')
    assert resp.get_json()['filename'] != 'pdf'


def test_distinct_non_latin_pdfs_do_not_collide(client):
    a = _upload_bytes(client, _pdf_bytes(1), 'коды.pdf', '/upload_pdf')
    b = _upload_bytes(client, _pdf_bytes(2), '代码.pdf', '/upload_pdf')
    assert a.get_json()['filename'] != b.get_json()['filename']


# ---------------------------------------------------------------------------
# D1-8 CSV decoding / parse failures / leftovers
# ---------------------------------------------------------------------------

def test_cp1252_csv_uploads_and_decodes(client):
    raw = 'name,city,quote\nJosé,Montréal,“hi”\nZoë,Köln,–\n'.encode('cp1252')
    resp = _upload_bytes(client, raw, 'latin.csv')
    assert resp.status_code == 200, resp.get_json()
    rows = post_json(client, '/data_page', {'filename': 'latin.csv'}).get_json()['data']
    assert rows[0]['name'] == 'José'
    assert rows[0]['quote'] == '“hi”'
    assert rows[1]['city'] == 'Köln'


def test_empty_csv_is_clear_422_and_leaves_nothing(client, app):
    resp = _upload_bytes(client, b'', 'empty.csv')
    assert resp.status_code == 422
    assert 'empty' in resp.get_json()['error'].lower()
    assert _identity_files(app, client) == []


def test_malformed_row_after_100_is_caught_at_upload(client, app):
    lines = ['a,b'] + [f'{i},{i}' for i in range(150)] + ['1,2,3,4,5'] + ['9,9']
    resp = _upload_bytes(client, '\n'.join(lines).encode(), 'bad.csv')
    assert resp.status_code == 422, resp.get_json()
    assert _identity_files(app, client) == []


def test_profile_failure_removes_file(client, app, monkeypatch):
    from statlee import datatools

    def boom(_df):
        raise RuntimeError('profile exploded')

    monkeypatch.setattr(datatools, 'profile_dataframe', boom)
    resp = _upload_bytes(client, SAMPLE_CSV.encode(), 'p.csv')
    assert resp.status_code >= 400
    assert _identity_files(app, client) == []


# ---------------------------------------------------------------------------
# D1-14 replace at quota
# ---------------------------------------------------------------------------

def test_reupload_same_name_at_file_quota_replaces(client, app):
    app.config['STATLEE'].max_datasets_per_identity = 1
    assert upload_csv(client, SAMPLE_CSV).status_code == 200
    again = upload_csv(client, 'age\n1\n')
    assert again.status_code == 200, again.get_json()
    assert again.get_json()['profile']['headers'] == ['age']


def test_new_name_at_file_quota_still_refused(client, app):
    app.config['STATLEE'].max_datasets_per_identity = 1
    assert upload_csv(client, SAMPLE_CSV).status_code == 200
    assert upload_csv(client, SAMPLE_CSV, filename='other.csv').status_code == 413


def test_reupload_resets_history_and_drops_old_versions(client, app):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle', {'filename': 'test.csv',
                                   'instruction': 'drop missing rows'})
    assert 'test__v2.csv' in _identity_files(app, client)
    body = upload_csv(client, SAMPLE_CSV).get_json()
    assert [v['v'] for v in body['changelog']['versions']] == [1]
    assert 'test__v2.csv' not in _identity_files(app, client)


# ---------------------------------------------------------------------------
# D1-10 TXT codebooks keep non-Latin text; PDF over cap is 413
# ---------------------------------------------------------------------------

def test_txt_codebook_keeps_non_latin_text(client, fake_llm):
    txt = 'age: Возраст респондента\nq1: “Don’t know” — 不知道\n'.encode()
    up = _upload_bytes(client, txt, 'codes.txt', '/upload_pdf')
    assert up.status_code == 200
    fake_llm.set('pdf_extract', '{"age": "Возраст"}')
    resp = post_json(client, '/extract_pdf_codebook',
                     {'filename': up.get_json()['filename']})
    assert resp.status_code == 200
    sent = [c[2] for c in fake_llm.calls if c[1] == 'pdf_extract'][0]
    assert 'Возраст респондента' in sent
    assert '“Don’t know” — 不知道' in sent


def test_ascii_pdf_over_page_cap_is_413(client, app):
    app.config['STATLEE'].pdf_max_pages = 2
    resp = _upload_bytes(client, _pdf_bytes(3), 'codes.pdf', '/upload_pdf')
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# D1-13 multi-sheet Excel and SPSS value labels
# ---------------------------------------------------------------------------

def _xlsx_bytes(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return buf.getvalue()


def test_multi_sheet_workbook_uses_data_sheet_and_warns(client):
    pytest.importorskip('openpyxl')
    raw = _xlsx_bytes({
        'README': pd.DataFrame({'a': ['see the data sheet']}),
        'data': pd.DataFrame({'age': [20, 30, 40], 'inc': [1, 2, 3]}),
    })
    resp = _upload_bytes(client, raw, 'book.xlsx')
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['profile']['headers'] == ['age', 'inc']
    assert any('README' in w for w in body['warnings'])


def test_single_sheet_workbook_has_no_warning(client):
    pytest.importorskip('openpyxl')
    raw = _xlsx_bytes({'Sheet1': pd.DataFrame({'age': [1, 2]})})
    body = _upload_bytes(client, raw, 'one.xlsx').get_json()
    assert body['warnings'] == []


def test_sav_value_labels_reach_labels(client, tmp_path):
    pyreadstat = pytest.importorskip('pyreadstat')
    src = tmp_path / 'survey.sav'
    df = pd.DataFrame({'gender': [1.0, 2.0, 1.0], 'age': [30.0, 40.0, 50.0],
                       'agree': [1.0, 5.0, 3.0]})
    pyreadstat.write_sav(
        df, str(src),
        column_labels={'gender': 'Respondent gender', 'age': 'Age'},
        variable_value_labels={'gender': {1.0: 'Male', 2.0: 'Female'},
                               'agree': {1.0: 'Strongly disagree',
                                         5.0: 'Strongly agree'}})
    resp = _upload_bytes(client, src.read_bytes(), 'survey.sav')
    assert resp.status_code == 200, resp.get_json()
    labels = resp.get_json()['labels']
    assert labels['gender'].startswith('Respondent gender')
    assert '1=Male' in labels['gender'] and '2=Female' in labels['gender']
    assert labels['age'] == 'Age'
    assert '1=Strongly disagree' in labels['agree']
