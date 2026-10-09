"""SP2 codebook routes and seeding: GET/PUT /codebook, import/export, the
seeds from native .sav metadata, /classify_variables and
/extract_pdf_codebook, sha256 restore on re-upload, and wrangle inheritance."""
import io
import json

import pandas as pd
import pytest
from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv

from statlee import codebook as cb
from statlee import storage


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _get(client, filename='test.csv'):
    return client.get('/codebook', query_string={'filename': filename})


def _put(client, payload, token=None):
    headers = {}
    if token is None:
        token = csrf_token(client)
    if token:
        headers['X-CSRF-Token'] = token
    return client.put('/codebook', json=payload, headers=headers)


def _upload_bytes(client, raw, filename):
    token = csrf_token(client)
    return client.post('/upload', data={'file': (io.BytesIO(raw), filename)},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})


def _upload_doc(client, text=b'Q1. Age in years', name='book.txt'):
    token = csrf_token(client)
    resp = client.post('/upload_pdf', data={'file': (io.BytesIO(text), name)},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})
    assert resp.status_code == 200
    return resp.get_json()['filename']


# ---------------------------------------------------------------------------
# GET /codebook
# ---------------------------------------------------------------------------

def test_get_returns_default_entries_in_column_order(client):
    upload_csv(client, SAMPLE_CSV)
    resp = _get(client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'success'
    assert body['filename'] == 'test.csv'
    assert body['columns'] == ['age', 'income', 'group']
    assert set(body['codebook']) == {'age', 'income', 'group'}
    assert body['codebook']['age'] == cb.default_entry()
    assert body['has_codebook'] is False
    assert body['dataset_version'] == 1
    assert len(body['sha256']) == 64
    assert body['options'] == {'types': list(cb.TYPES),
                               'roles': list(cb.ROLES),
                               'sources': list(cb.SOURCES) + ['default'],
                               'fields': list(cb.FIELDS)}


@pytest.mark.parametrize('query,status', [
    ({}, 400),
    ({'filename': 'nope.csv'}, 404),
    ({'filename': 'test__v2.csv'}, 400),
    ({'filename': '../../etc/passwd'}, 404),
])
def test_get_rejects_missing_unknown_and_reserved_names(client, query, status):
    upload_csv(client, SAMPLE_CSV)
    assert client.get('/codebook', query_string=query).status_code == status


def test_codebook_is_identity_scoped(app, client):
    upload_csv(client, SAMPLE_CSV)
    assert _put(client, {'filename': 'test.csv',
                         'codebook': {'age': {'label': 'Mine'}}}
                ).status_code == 200
    other = app.test_client()
    assert other.get('/codebook',
                     query_string={'filename': 'test.csv'}).status_code == 404
    assert _put(other, {'filename': 'test.csv',
                        'codebook': {'age': {'label': 'Theirs'}}}
                ).status_code == 404
    assert _get(client).get_json()['codebook']['age']['label'] == 'Mine'


# ---------------------------------------------------------------------------
# PUT /codebook
# ---------------------------------------------------------------------------

def test_put_partial_update_marks_fields_user(client):
    upload_csv(client, SAMPLE_CSV)
    resp = _put(client, {'filename': 'test.csv', 'codebook': {
        'group': {'type': 'nominal', 'label': 'Treatment arm',
                  'value_labels': {'A': 'Control', 'B': 'Drug'}},
        'age': {'missing_codes': [-9], 'role': 'analyze'}}})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    group = body['codebook']['group']
    assert group['label'] == 'Treatment arm'
    assert group['source'] == 'user'
    assert group['user_fields'] == ['type', 'label', 'value_labels']
    assert body['codebook']['age']['missing_codes'] == ['-9']
    assert body['codebook']['income'] == cb.default_entry()
    assert body['has_codebook'] is True
    # A second partial PUT keeps the first one's fields.
    _put(client, {'filename': 'test.csv',
                  'codebook': {'group': {'scale_group': 'arms'}}})
    group = _get(client).get_json()['codebook']['group']
    assert group['label'] == 'Treatment arm' and group['scale_group'] == 'arms'


def test_put_accepts_an_entry_sent_back_as_received(client):
    upload_csv(client, SAMPLE_CSV)
    entry = _get(client).get_json()['codebook']['age']
    entry = dict(entry, type='continuous')
    resp = _put(client, {'filename': 'test.csv', 'codebook': {'age': entry}})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['codebook']['age']['type'] == 'continuous'


@pytest.mark.parametrize('payload', [
    {'filename': 'test.csv'},
    {'filename': 'test.csv', 'codebook': {}},
    {'filename': 'test.csv', 'codebook': ['age']},
    {'filename': 'test.csv', 'codebook': {'nope': {'label': 'x'}}},
    {'filename': 'test.csv', 'codebook': {'age': {'type': 'interval'}}},
    {'filename': 'test.csv', 'codebook': {'age': {'role': 'outcome'}}},
    {'filename': 'test.csv', 'codebook': {'age': {'colour': 'red'}}},
    {'filename': 'test.csv', 'codebook': {'age': 'Continuous'}},
    {'filename': 5, 'codebook': {'age': {'label': 'x'}}},
    {'codebook': {'age': {'label': 'x'}}},
])
def test_put_rejects_invalid_payloads(client, payload):
    upload_csv(client, SAMPLE_CSV)
    resp = _put(client, payload)
    assert resp.status_code == 400, resp.get_json()
    assert 'error' in resp.get_json()


def test_put_unknown_dataset_is_404_and_needs_csrf(client):
    upload_csv(client, SAMPLE_CSV)
    assert _put(client, {'filename': 'nope.csv',
                         'codebook': {'age': {'label': 'x'}}}
                ).status_code == 404
    assert _put(client, {'filename': 'test.csv',
                         'codebook': {'age': {'label': 'x'}}},
                token='').status_code == 403


def test_put_clamps_long_values(client):
    upload_csv(client, SAMPLE_CSV)
    resp = _put(client, {'filename': 'test.csv', 'codebook': {
        'age': {'label': 'x' * 50000, 'scale_group': 'g' * 500}}})
    assert resp.status_code == 200
    age = resp.get_json()['codebook']['age']
    assert len(age['label']) == cb.LABEL_MAX
    assert len(age['scale_group']) == cb.SCALE_GROUP_MAX


# ---------------------------------------------------------------------------
# Seeds: classification, PDF extraction, native .sav metadata
# ---------------------------------------------------------------------------

def test_classify_writes_llm_layer_without_overriding_user_type(
        client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv',
                  'codebook': {'group': {'type': 'binary'}}})
    fake_llm.set('classify', json.dumps(
        {'age': 'Continuous', 'group': 'Nominal', 'income': 'continuous'}))
    resp = post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert resp.status_code == 200
    # Back-compat: the response still carries the model's levels.
    assert resp.get_json()['codebook']['group'] == 'Nominal'
    book = _get(client).get_json()['codebook']
    assert book['age']['type'] == 'continuous'
    assert book['age']['source'] == 'llm'
    assert book['group']['type'] == 'binary'            # user edit kept
    assert book['group']['field_sources'] == {'type': 'user'}
    # A re-classify (cached) still never overrides the user's field.
    post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert _get(client).get_json()['codebook']['group']['type'] == 'binary'


def test_pdf_extraction_seeds_the_named_dataset(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv',
                  'codebook': {'income': {'label': 'My income label'}}})
    fake_llm.set('pdf_extract', json.dumps({
        'AGE': 'Age in years', 'income': 'Household income',
        'unrelated': 'Not a column'}))
    doc = _upload_doc(client)
    resp = post_json(client, '/extract_pdf_codebook',
                     {'filename': doc, 'dataset': 'test.csv'})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['mapping']['AGE'] == 'Age in years'     # unchanged contract
    assert body['codebook_updated'] == 2
    book = _get(client).get_json()['codebook']
    assert book['age']['label'] == 'Age in years'
    assert book['age']['source'] == 'pdf'
    assert book['income']['label'] == 'My income label'  # user beats pdf


def test_pdf_extraction_without_dataset_writes_nothing(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('pdf_extract', json.dumps({'age': 'Age in years'}))
    doc = _upload_doc(client)
    resp = post_json(client, '/extract_pdf_codebook', {'filename': doc})
    assert resp.status_code == 200
    assert 'codebook_updated' not in resp.get_json()
    assert _get(client).get_json()['has_codebook'] is False


def test_pdf_extraction_unknown_dataset_is_404_before_any_llm_call(
        client, fake_llm):
    doc = _upload_doc(client)
    resp = post_json(client, '/extract_pdf_codebook',
                     {'filename': doc, 'dataset': 'nope.csv'})
    assert resp.status_code == 404
    assert not [c for c in fake_llm.calls if c[1] == 'pdf_extract']


def test_survey_extraction_seeds_too(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('survey_extract', json.dumps({'group': 'Q3 arm (A/B)'}))
    doc = _upload_doc(client, b'Q3. Which arm?', 'survey.txt')
    post_json(client, '/extract_pdf_codebook', {
        'filename': doc, 'mode': 'survey', 'dataset': 'test.csv',
        'headers': ['age', 'income', 'group']})
    assert _get(client).get_json()['codebook']['group']['label'] == \
        'Q3 arm (A/B)'


def _sav_bytes(tmp_path, missing=True):
    pyreadstat = pytest.importorskip('pyreadstat')
    src = tmp_path / 'survey.sav'
    df = pd.DataFrame({'gender': [1.0, 2.0, -9.0, 1.0],
                       'age': [30.0, 40.0, 50.0, 99.0],
                       'agree': [1.0, 5.0, 3.0, 2.0]})
    kwargs = {}
    if missing:
        kwargs['missing_ranges'] = {'gender': [-9],
                                    'age': [{'lo': 98, 'hi': 99}]}
    pyreadstat.write_sav(
        df, str(src),
        column_labels={'gender': 'Respondent gender', 'age': 'Age'},
        variable_value_labels={'gender': {1.0: 'Male', 2.0: 'Female',
                                          -9.0: 'Refused'}},
        **kwargs)
    return src.read_bytes()


def test_sav_upload_seeds_native_labels_value_labels_and_missing(
        client, tmp_path):
    resp = _upload_bytes(client, _sav_bytes(tmp_path), 'survey.sav')
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['labels']['gender'].startswith('Respondent gender')
    book = _get(client, 'survey.csv').get_json()['codebook']
    gender = book['gender']
    assert gender['label'] == 'Respondent gender'
    assert gender['value_labels'] == {'1': 'Male', '2': 'Female',
                                      '-9': 'Refused'}
    assert gender['missing_codes'] == ['-9']
    assert gender['source'] == 'native'
    assert book['age']['missing_codes'] == ['98 to 99']
    assert book['agree']['source'] == 'default'


def test_native_beats_llm_and_pdf_but_not_user(client, tmp_path, fake_llm):
    _upload_bytes(client, _sav_bytes(tmp_path), 'survey.sav')
    fake_llm.set('pdf_extract', json.dumps({'gender': 'PDF gender'}))
    doc = _upload_doc(client)
    post_json(client, '/extract_pdf_codebook',
              {'filename': doc, 'dataset': 'survey.csv'})
    assert _get(client, 'survey.csv').get_json()['codebook']['gender'][
        'label'] == 'Respondent gender'
    _put(client, {'filename': 'survey.csv',
                  'codebook': {'gender': {'label': 'Sex'}}})
    assert _get(client, 'survey.csv').get_json()['codebook']['gender'][
        'label'] == 'Sex'


# ---------------------------------------------------------------------------
# sha256 restore on re-upload
# ---------------------------------------------------------------------------

def test_reupload_of_identical_content_restores_the_codebook(client):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv',
                  'codebook': {'age': {'label': 'Kept'}}})
    resp = upload_csv(client, SAMPLE_CSV)
    assert resp.get_json()['codebook_restored'] is True
    assert _get(client).get_json()['codebook']['age']['label'] == 'Kept'


def test_reupload_of_different_content_starts_fresh(client):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv',
                  'codebook': {'age': {'label': 'Old data'}}})
    resp = upload_csv(client, SAMPLE_CSV + '50,1,A\n')
    assert resp.get_json()['codebook_restored'] is False
    assert _get(client).get_json()['codebook']['age']['label'] == ''


def test_identical_content_restores_after_the_name_held_other_data(client):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv',
                  'codebook': {'age': {'label': 'Original'}}})
    upload_csv(client, SAMPLE_CSV + '50,1,A\n')      # replaces the sidecar
    upload_csv(client, SAMPLE_CSV, filename='again.csv')
    assert _get(client, 'again.csv').get_json()['codebook']['age'][
        'label'] == 'Original'
    upload_csv(client, SAMPLE_CSV)
    assert _get(client).get_json()['codebook']['age']['label'] == 'Original'


def test_archive_is_pruned(app, client, monkeypatch):
    monkeypatch.setattr(storage, 'CODEBOOK_ARCHIVE_MAX', 2)
    ident = _identity(client)
    with app.app_context():
        for i in range(4):
            storage.save_codebook_archive(f'{i:064x}', {'layers': {}},
                                          identity=ident)
        assert storage.load_codebook_archive(f'{3:064x}', ident) is not None
        assert storage.load_codebook_archive(f'{0:064x}', ident) is None
        assert storage.load_codebook_archive('../evil', ident) is None


# ---------------------------------------------------------------------------
# Wrangle versions inherit the codebook
# ---------------------------------------------------------------------------

def test_wrangle_versions_inherit_add_defaults_and_drop_removed(
        client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv', 'codebook': {
        'age': {'label': 'Age'}, 'income': {'label': 'Income'}}})
    fake_llm.set('wrangle', json.dumps({
        'code': "df = df.drop(columns=['income'])\ndf['age2'] = df['age'] * 2",
        'summary': 'Dropped income, added age2', 'error': None}))
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop income'})
    assert resp.status_code == 200, resp.get_json()
    body = _get(client).get_json()
    assert body['dataset_version'] == 2
    assert body['columns'] == ['age', 'group', 'age2']
    assert set(body['codebook']) == {'age', 'group', 'age2'}
    assert body['codebook']['age']['label'] == 'Age'
    assert body['codebook']['age2'] == cb.default_entry()
    post_json(client, '/version_control', {'filename': 'test.csv',
                                           'direction': 'undo'})
    assert _get(client).get_json()['codebook']['income']['label'] == 'Income'


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

def test_export_downloads_the_codebook_document(client):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv',
                  'codebook': {'age': {'label': 'Age', 'role': 'analyze'}}})
    resp = client.get('/codebook/export', query_string={'filename': 'test.csv'})
    assert resp.status_code == 200
    assert resp.mimetype == 'application/json'
    assert 'attachment' in resp.headers['Content-Disposition']
    assert 'test_codebook.json' in resp.headers['Content-Disposition']
    doc = json.loads(resp.get_data(as_text=True))
    assert doc['format'] == cb.EXPORT_FORMAT and doc['version'] == 1
    assert doc['dataset'] == 'test.csv'
    assert doc['dataset_version'] == 1 and len(doc['sha256']) == 64
    assert doc['codebook']['age']['label'] == 'Age'
    assert list(doc['codebook']) == ['age', 'income', 'group']
    assert client.get('/codebook/export',
                      query_string={'filename': 'nope.csv'}).status_code == 404


def test_export_document_function_is_plain_json(app, client):
    upload_csv(client, SAMPLE_CSV)
    ident = _identity(client)
    with app.app_context():
        doc = cb.export_document('test.csv', identity=ident)
        assert json.loads(json.dumps(doc)) == doc
        assert cb.export_document('nope.csv', identity=ident) is None


def test_import_round_trips_an_export(app, client):
    upload_csv(client, SAMPLE_CSV)
    _put(client, {'filename': 'test.csv', 'codebook': {
        'group': {'type': 'nominal', 'value_labels': {'A': 'Control'}}}})
    doc = json.loads(client.get('/codebook/export', query_string={
        'filename': 'test.csv'}).get_data(as_text=True))
    doc['codebook']['extra_col'] = {'label': 'not here'}

    other = app.test_client()
    upload_csv(other, SAMPLE_CSV, filename='copy.csv')
    resp = post_json(other, '/codebook/import',
                     {'filename': 'copy.csv', 'codebook': doc})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['ignored'] == ['extra_col']
    assert body['imported'] == 1            # only the user-edited column
    group = body['codebook']['group']
    assert group['value_labels'] == {'A': 'Control'}
    assert group['source'] == 'user'
    assert group['user_fields'] == ['type', 'value_labels']
    assert body['codebook']['age'] == cb.default_entry()


def test_import_keeps_the_targets_native_labels(app, client, tmp_path):
    """A CSV copy's export (defaults plus one user edit) imported into the
    .sav dataset: the native value labels and missing codes survive."""
    _upload_bytes(client, _sav_bytes(tmp_path), 'survey.sav')
    other = app.test_client()
    upload_csv(other, 'gender,age,agree\n1,30,1\n2,40,5\n',
               filename='plain.csv')
    _put(other, {'filename': 'plain.csv',
                 'codebook': {'age': {'label': 'Age (years)'}}})
    doc = json.loads(other.get('/codebook/export', query_string={
        'filename': 'plain.csv'}).get_data(as_text=True))
    resp = post_json(client, '/codebook/import',
                     {'filename': 'survey.csv', 'codebook': doc})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['imported'] == 1
    gender = body['codebook']['gender']
    assert gender['value_labels'] == {'1': 'Male', '2': 'Female',
                                      '-9': 'Refused'}
    assert gender['missing_codes'] == ['-9']
    assert gender['label'] == 'Respondent gender'
    assert gender['source'] == 'native' and gender['user_fields'] == []
    assert body['codebook']['age']['label'] == 'Age (years)'
    assert body['codebook']['age']['missing_codes'] == ['98 to 99']


def test_corrupt_sidecar_is_never_overwritten(app, client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    ident = _identity(client)
    with app.app_context():
        path = storage._meta_path('test.csv', identity=ident)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('{"sha256": "abc", "original_na')
    resp = _put(client, {'filename': 'test.csv',
                         'codebook': {'age': {'label': 'x'}}})
    assert resp.status_code == 500
    assert 'error' in resp.get_json()
    with open(path, encoding='utf-8') as fh:
        assert fh.read() == '{"sha256": "abc", "original_na'
    with app.app_context():
        with pytest.raises(cb.CodebookStorageError):
            cb.apply('test.csv', 'llm', {'age': {'type': 'nominal'}},
                     identity=ident)
    # A seed never fails its route over it: classification still answers.
    fake_llm.set('classify', json.dumps({'age': 'Continuous'}))
    assert post_json(client, '/classify_variables',
                     {'filename': 'test.csv'}).status_code == 200
    with open(path, encoding='utf-8') as fh:
        assert fh.read() == '{"sha256": "abc", "original_na'


@pytest.mark.parametrize('payload,status', [
    ({'filename': 'test.csv'}, 400),
    ({'filename': 'test.csv', 'codebook': 'x'}, 400),
    ({'filename': 'test.csv', 'codebook': {'age': {'type': 'bogus'}}}, 400),
    ({'filename': 'nope.csv', 'codebook': {'age': {'label': 'x'}}}, 404),
])
def test_import_rejects_invalid_payloads(client, payload, status):
    upload_csv(client, SAMPLE_CSV)
    assert post_json(client, '/codebook/import', payload).status_code == status


def test_import_needs_csrf(client):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/codebook/import',
                     {'filename': 'test.csv',
                      'codebook': {'age': {'label': 'x'}}}, token='')
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Final review item 4b: the stored codebook has a total size cap
# ---------------------------------------------------------------------------

def test_codebook_over_the_total_size_cap_is_rejected(client, monkeypatch):
    upload_csv(client, SAMPLE_CSV)
    monkeypatch.setattr(cb, 'CODEBOOK_BYTES_MAX', 3000)
    ok = _put(client, {'filename': 'test.csv',
                       'codebook': {'age': {'label': 'Age in years'}}})
    assert ok.status_code == 200
    resp = _put(client, {'filename': 'test.csv', 'codebook': {
        'age': {'label': 'a' * 1500}, 'income': {'label': 'b' * 1500}}})
    assert resp.status_code == 413
    assert 'too large' in resp.get_json()['error']
    # Nothing was written: the earlier label stands.
    assert _get(client).get_json()['codebook']['age']['label'] == \
        'Age in years'


def test_codebook_size_cap_applies_to_every_writer(app, client, monkeypatch):
    upload_csv(client, SAMPLE_CSV)
    ident = _identity(client)
    monkeypatch.setattr(cb, 'CODEBOOK_BYTES_MAX', 500)
    with app.app_context():
        with pytest.raises(cb.CodebookTooLarge):
            cb.apply('test.csv', 'llm', {'age': {'label': 'x' * 600}},
                     identity=ident)


def test_codebook_already_over_the_cap_can_still_shrink(client, monkeypatch):
    upload_csv(client, SAMPLE_CSV)
    assert _put(client, {'filename': 'test.csv', 'codebook': {
        'age': {'label': 'a' * 1500}}}).status_code == 200
    monkeypatch.setattr(cb, 'CODEBOOK_BYTES_MAX', 500)
    resp = _put(client, {'filename': 'test.csv',
                         'codebook': {'age': {'label': 'short'}}})
    assert resp.status_code == 200
    assert _put(client, {'filename': 'test.csv', 'codebook': {
        'income': {'label': 'b' * 600}}}).status_code == 413
