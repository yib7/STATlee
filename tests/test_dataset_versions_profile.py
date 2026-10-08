"""/dataset_versions carries the active version's headers and profile (S5).

After a /wrangle 409 the client refreshes the changelog from /dataset_versions;
it used to infer the columns from a one-row /data_page probe as well (Flask
sorts JSON keys, so new columns came back in alphabetical order, and an empty
result kept the old headers). The route now returns them, in frame order.
"""
import json

from conftest import SAMPLE_CSV, post_json, upload_csv


def _plan(fake_llm, code, summary):
    fake_llm.set('wrangle', json.dumps({'code': code, 'summary': summary,
                                        'error': None}))


def test_dataset_versions_returns_active_headers_and_profile(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    _plan(fake_llm, code="df['age2'] = df['age'] * 2", summary='double age')
    assert post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'double age'}
                     ).status_code == 200
    body = post_json(client, '/dataset_versions',
                     {'filename': 'test.csv'}).get_json()
    assert body['changelog']['active'] == 2            # existing field kept
    assert body['headers'] == ['age', 'income', 'group', 'age2']
    assert body['profile']['headers'] == body['headers']
    assert body['profile']['total_columns'] == 4
    assert 'age2' in body['profile']['numeric_columns']
