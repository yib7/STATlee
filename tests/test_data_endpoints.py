"""Data viewer JSON validity and the /suggest cache key (deep audit D1-9,
D1-11, latent L5)."""
import json

from conftest import SAMPLE_CSV, post_json, upload_csv


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


def test_suggest_cache_respects_pdf_mapping(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    cb = {'age': 'Continuous'}
    first = post_json(client, '/suggest', {'filename': 'test.csv', 'codebook': cb})
    assert not first.get_json().get('cached')
    second = post_json(client, '/suggest', {
        'filename': 'test.csv', 'codebook': cb,
        'pdf_mapping': {'age': 'Age in years at interview'}})
    assert not second.get_json().get('cached')
    third = post_json(client, '/suggest', {
        'filename': 'test.csv', 'codebook': cb,
        'pdf_mapping': {'age': 'Age in years at interview'}})
    assert third.get_json().get('cached') is True


def test_suggest_cache_distinguishes_codebooks_past_512_chars(client):
    upload_csv(client, SAMPLE_CSV)
    base = {f'col_{i:03d}': 'Continuous' for i in range(40)}
    other = dict(base, col_039='Nominal')
    post_json(client, '/suggest', {'filename': 'test.csv', 'codebook': base})
    resp = post_json(client, '/suggest', {'filename': 'test.csv',
                                          'codebook': other})
    assert not resp.get_json().get('cached')
