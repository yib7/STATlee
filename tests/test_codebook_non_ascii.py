"""Non-ASCII column names round-trip through the codebook (ship checklist
3.13): GET/PUT /codebook, GET /codebook/export, /codebook/import into another
session, and codebook.json in the project export."""
import io
import json
import zipfile

from conftest import csrf_token, post_json, upload_csv

COLUMNS = ['âge', '收入', 'группа', 'نتيجة']
CSV = ','.join(COLUMNS) + '\n25,30000,A,1\n40,55000,B,0\n'


def _put(client, payload):
    return client.put('/codebook', json=payload,
                      headers={'X-CSRF-Token': csrf_token(client)})


def _get(client):
    return client.get('/codebook', query_string={'filename': 'test.csv'})


def test_non_ascii_columns_round_trip_through_get_and_put(client):
    upload_csv(client, CSV)
    body = _get(client).get_json()
    assert body['columns'] == COLUMNS
    resp = _put(client, {'filename': 'test.csv', 'codebook': {
        '收入': {'label': 'Доход в месяц (€)', 'type': 'continuous'},
        'группа': {'value_labels': {'A': 'Gruppe Ä', 'B': 'グループB'},
                   'role': 'analyze'},
        'نتيجة': {'missing_codes': ['-9'], 'scale_group': 'échelle'}}})
    assert resp.status_code == 200, resp.get_json()
    book = _get(client).get_json()['codebook']
    assert book['收入']['label'] == 'Доход в месяц (€)'
    assert book['收入']['type'] == 'continuous'
    assert book['группа']['value_labels'] == {'A': 'Gruppe Ä',
                                             'B': 'グループB'}
    assert book['نتيجة']['missing_codes'] == ['-9']
    assert book['نتيجة']['scale_group'] == 'échelle'
    assert book['âge']['source'] == 'default'


def test_non_ascii_columns_survive_export_and_import(app, client):
    upload_csv(client, CSV)
    assert _put(client, {'filename': 'test.csv', 'codebook': {
        '收入': {'label': 'Доход'},
        'âge': {'label': 'Âge en années'}}}).status_code == 200

    exported = client.get('/codebook/export',
                          query_string={'filename': 'test.csv'})
    assert exported.status_code == 200
    raw = exported.get_data(as_text=True)
    assert '收入' in raw and 'Доход' in raw      # stored as text, not \u escapes
    doc = json.loads(raw)
    assert doc['codebook']['âge']['label'] == 'Âge en années'

    # Another session with the same columns imports it back.
    other = app.test_client()
    upload_csv(other, CSV)
    resp = post_json(other, '/codebook/import',
                     {'filename': 'test.csv', 'codebook': doc})
    assert resp.status_code == 200, resp.get_json()
    book = other.get('/codebook',
                     query_string={'filename': 'test.csv'}).get_json()
    assert book['codebook']['收入']['label'] == 'Доход'
    assert book['codebook']['âge']['label'] == 'Âge en années'


def test_non_ascii_columns_in_the_bundle_codebook_json(client):
    upload_csv(client, CSV)
    assert _put(client, {'filename': 'test.csv', 'codebook': {
        'группа': {'label': 'Группа участников'}}}).status_code == 200
    resp = post_json(client, '/export', {'filename': 'test.csv'})
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    raw = zf.read('codebook.json').decode('utf-8')
    assert 'Группа участников' in raw
    book = json.loads(raw)['codebook']
    assert list(book) == COLUMNS
    assert book['группа']['label'] == 'Группа участников'
    # The shipped dataset keeps the same header.
    assert zf.read('test.csv').decode('utf-8').splitlines()[0] == \
        ','.join(COLUMNS)
