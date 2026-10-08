"""Data viewer JSON validity and the /suggest cache key (deep audit D1-9,
D1-11, latent L5)."""
import json

from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv


def _put_codebook(client, codebook, filename='test.csv'):
    """Edit the server codebook (SP2): the only codebook prompts read."""
    token = csrf_token(client)
    resp = client.put('/codebook', json={'filename': filename,
                                         'codebook': codebook},
                      headers={'X-CSRF-Token': token})
    assert resp.status_code == 200, resp.get_json()


def test_data_page_never_emits_infinity_or_nan(client):
    upload_csv(client, 'x,ratio\n1,inf\n2,-inf\n3,\n4,1.5\n')
    resp = post_json(client, '/data_page', {'filename': 'test.csv'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Infinity' not in body
    assert 'NaN' not in body
    # Strict parse, like the browser's JSON.parse.
    data = json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError(c)))['data']
    assert data[0]['ratio'] is None
    assert data[1]['ratio'] is None
    assert data[2]['ratio'] is None
    assert data[3]['ratio'] == 1.5


def test_data_page_row_cap_does_not_parse_whole_file(client, app, monkeypatch):
    from statlee.routes import datasets as datasets_mod
    app.config['STATLEE'].data_page_row_cap = 3
    upload_csv(client, 'x\n' + '\n'.join(str(i) for i in range(50)) + '\n')
    datasets_mod.clear_caches()
    seen = []
    real = datasets_mod.pd.read_csv

    def spy(*a, **k):
        seen.append(k.get('nrows'))
        return real(*a, **k)

    monkeypatch.setattr(datasets_mod.pd, 'read_csv', spy)
    resp = post_json(client, '/data_page', {'filename': 'test.csv'})
    assert resp.status_code == 413
    assert seen and all(n is not None for n in seen), seen


def test_suggest_cache_respects_codebook_descriptions(client, fake_llm):
    """D1-11 on the server codebook (SP2): a newly linked description (a pdf
    seed or a user label) serves fresh suggestions; the same context hits."""
    upload_csv(client, SAMPLE_CSV)
    _put_codebook(client, {'age': {'type': 'continuous'}})
    first = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert not first.get_json().get('cached')
    _put_codebook(client, {'age': {'label': 'Age in years at interview'}})
    second = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert not second.get_json().get('cached')
    third = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert third.get_json().get('cached') is True


def test_suggest_cache_distinguishes_codebooks_past_512_chars(client):
    cols = [f'col_{i:03d}' for i in range(40)]
    upload_csv(client, ','.join(cols) + '\n' + ','.join('1' for _ in cols)
               + '\n')
    _put_codebook(client, {c: {'type': 'continuous'} for c in cols})
    post_json(client, '/suggest', {'filename': 'test.csv'})
    _put_codebook(client, {'col_039': {'type': 'nominal'}})
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert not resp.get_json().get('cached')
