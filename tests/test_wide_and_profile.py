"""Dataset profile honesty (deep audit D3-7) and wide datasets (D1-5 / D3-6).

* Missing counts / dtypes / ranges are computed on the FULL frame and the true
  row count is reported; only the sample values may stay sampled.
* Feature selection sees every column (annotated by the codebook where one
  exists), the draft's metadata covers the columns it picked, and
  /classify_variables reaches columns past the 80th.
"""
import json

import pandas as pd
from conftest import csrf_token, post_json, sse_events, upload_csv

from statlee import datatools


def _late_missing_csv():
    # 500 rows; x is present in the first 100 rows and missing in the other 400.
    xs = [str(i) for i in range(100)] + [''] * 400
    rows = [f'{i},{x}' for i, x in enumerate(xs)]
    return 'id,x\n' + '\n'.join(rows) + '\n'


def _wide_csv(n_cols=100, n_rows=3):
    header = ','.join(f'c{i}' for i in range(n_cols))
    rows = [','.join(str(r * i) for i in range(n_cols)) for r in range(n_rows)]
    return header + '\n' + '\n'.join(rows) + '\n'


def _prompt(fake_llm, kind):
    return [c[2] for c in fake_llm.calls if c[1] == kind]


# ---------------------------------------------------------------------------
# D3-7 profile honesty
# ---------------------------------------------------------------------------

def test_summary_reports_true_rows_and_full_frame_missingness():
    df = pd.DataFrame({'x': list(range(100)) + [None] * 400})
    summary = datatools.summarize_dataframe(df)
    assert summary['__dataset__']['rows'] == 500
    assert summary['x']['missing'] == 400
    assert summary['x']['missing_pct'] == 80.0


def test_chat_draft_metadata_is_not_from_first_100_rows(client, fake_llm):
    upload_csv(client, _late_missing_csv())
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'regress x on id'}))
    draft = _prompt(fake_llm, 'draft')[0]
    assert '"missing": 400' in draft
    assert '"rows": 500' in draft


def test_classify_and_suggest_see_full_frame(client, fake_llm):
    upload_csv(client, _late_missing_csv())
    post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert '"missing": 400' in _prompt(fake_llm, 'classify')[0]
    post_json(client, '/suggest', {'filename': 'test.csv'})
    assert 'missing=400' in _prompt(fake_llm, 'suggest')[0]


def test_wrangle_metadata_sees_full_frame(client, fake_llm):
    upload_csv(client, _late_missing_csv())
    post_json(client, '/wrangle', {'filename': 'test.csv',
                                   'instruction': 'fill missing x with 0'})
    assert '"missing": 400' in _prompt(fake_llm, 'wrangle')[0]


# ---------------------------------------------------------------------------
# D1-5 / D3-6 wide datasets
# ---------------------------------------------------------------------------

def test_feature_selection_sees_columns_past_80_with_codebook(client, fake_llm):
    upload_csv(client, _wide_csv())
    token = csrf_token(client)
    assert client.put('/codebook', json={
        'filename': 'test.csv',
        'codebook': {f'c{i}': {'type': 'continuous'} for i in range(80)}},
        headers={'X-CSRF-Token': token}).status_code == 200
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'model the last column'}))
    ctx = _prompt(fake_llm, 'feature_selection')[0]
    assert 'c95' in ctx
    assert 'c3' in ctx and 'Continuous' in ctx


def test_draft_metadata_covers_selected_columns_only(client, fake_llm):
    upload_csv(client, _wide_csv())
    fake_llm.set('feature_selection',
                 json.dumps({'required_columns': ['c95', 'c96']}))
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'correlate c95 and c96'}))
    draft = _prompt(fake_llm, 'draft')[0]
    assert '"c95": {' in draft
    assert '"c50": {' not in draft


def test_classify_reaches_columns_past_80(client, fake_llm):
    upload_csv(client, _wide_csv())
    fake_llm.set('classify', json.dumps({'c95': 'Nominal', 'c1': 'Continuous'}))
    resp = post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert resp.status_code == 200
    codebook = resp.get_json()['codebook']
    assert codebook.get('c95') == 'Nominal'
    assert codebook.get('c1') == 'Continuous'
    prompts = _prompt(fake_llm, 'classify')
    assert any('"c95"' in p for p in prompts)
    assert all('__truncated__' not in p for p in prompts)
