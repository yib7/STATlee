"""Request-shape validation and prompt-size caps (deep audit D1-6 / D3-1 and
D1-12).

* Wrong-typed JSON fields must come back as a 400, never a 500 from deep inside
  a route, and a bad ``language`` must not be persisted before validation.
* Every free-text field that reaches a paid prompt is capped, mirroring the
  P1-7 ``clamp`` style /chat already uses.
"""
import pytest
from conftest import SAMPLE_CSV, post_json, upload_csv

BIG = 'x' * 1_000_000


def _prompt_chars(fake_llm, kind):
    return [len(c[2]) for c in fake_llm.calls if c[1] == kind]


# ---------------------------------------------------------------------------
# D1-12: wrong-typed fields -> 400
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path,payload', [
    ('/method_prompt', {'filename': 'test.csv', 'method': 'OLS'}),
    ('/method_prompt', {'filename': 'test.csv',
                        'method': {'name': 'OLS'}, 'codebook': ['a']}),
    ('/suggest', {'filename': 'test.csv', 'codebook': ['a']}),
    ('/suggest', {'filename': 'test.csv', 'pdf_mapping': 'nope'}),
    ('/suggest', {'filename': 'test.csv', 'previous': 'one string'}),
    ('/wrangle', {'filename': 'test.csv', 'instruction': 5}),
    ('/report_issue', {'description': 'ok', 'code': {'a': 1}}),
    ('/report_issue', {'description': 123}),
    ('/run', {'code': ['x'], 'language': 'Python'}),
    ('/run', {'code': "print('x')", 'language': 5}),
    ('/run', {'code': "print('x')", 'language': 'Julia'}),
    ('/chat', {'filename': ['a'], 'prompt': 'hi'}),
    ('/chat', {'filename': 'test.csv', 'prompt': 'hi', 'language': 'Julia'}),
    ('/export', {'filename': ['a']}),
    ('/export', {'filename': 'test.csv', 'language': 7}),
    ('/data_page', {'filename': {'a': 1}}),
    ('/extract_pdf_codebook', {'filename': 'x.pdf', 'headers': 'age'}),
    ('/classify_variables', {'filename': 3}),
    ('/version_control', {'filename': ['a'], 'direction': 'undo'}),
])
def test_wrong_typed_field_is_400(client, path, payload):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, path, payload)
    assert resp.status_code == 400, (path, payload, resp.status_code,
                                     resp.get_data(as_text=True)[:200])
    assert 'error' in resp.get_json()


@pytest.mark.parametrize('path', ['/chat', '/run', '/wrangle', '/suggest',
                                  '/export', '/data_page', '/converse',
                                  '/interpret', '/generate_report',
                                  '/report_issue', '/method_prompt'])
def test_non_object_json_body_is_not_500(client, path):
    """A JSON array/scalar body used to hit ``data.get`` on a list -> 500."""
    resp = post_json(client, path, [1, 2, 3])
    assert resp.status_code < 500, (path, resp.status_code)


def test_bad_language_is_not_persisted_before_validation(client, fake_llm):
    """D1-12 side effect: /run used to save_approved_script(code, 5) before it
    crashed, poisoning every later /export with a ``5 .lower()`` 500."""
    upload_csv(client, SAMPLE_CSV)
    bad = post_json(client, '/run', {'code': "print('x')", 'language': 5,
                                     'filename': 'test.csv'})
    assert bad.status_code == 400
    exp = post_json(client, '/export', {'filename': 'test.csv'})
    assert exp.status_code == 200


# ---------------------------------------------------------------------------
# D1-6 / D3-1: caps on prompt-bound client text
# ---------------------------------------------------------------------------

def test_wrangle_instruction_is_capped(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle', {'filename': 'test.csv', 'instruction': BIG})
    for kind in ('moderation', 'wrangle'):
        sizes = _prompt_chars(fake_llm, kind)
        assert sizes, kind
        assert max(sizes) < 50_000, (kind, sizes)


def test_wrangle_instruction_stored_capped(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/wrangle',
                     {'filename': 'test.csv', 'instruction': BIG})
    assert resp.status_code == 200
    versions = resp.get_json()['changelog']['versions']
    assert len(versions[-1]['instruction']) <= 5000


def test_suggest_inputs_are_capped(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/suggest', {
        'filename': 'test.csv',
        'codebook': {'age': BIG, 'income': 'Continuous'},
        'pdf_mapping': {'age': BIG, 'group': 'Treatment arm'},
        'previous': [BIG] * 5 + ['short'] * 50,
    })
    assert resp.status_code == 200, resp.get_json()
    sizes = _prompt_chars(fake_llm, 'suggest')
    assert sizes and max(sizes) < 60_000, (sizes, [c[1] for c in fake_llm.calls])


def test_method_prompt_inputs_are_capped(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/method_prompt', {
        'filename': 'test.csv',
        'method': {'name': BIG, 'description': BIG},
        'codebook': {'age': BIG},
        'pdf_mapping': {'age': BIG},
    })
    sizes = _prompt_chars(fake_llm, 'method_prompt')
    assert sizes and max(sizes) < 60_000, sizes


def test_extract_pdf_codebook_headers_are_capped(client, fake_llm):
    import io

    from conftest import csrf_token
    token = csrf_token(client)
    up = client.post('/upload_pdf',
                     data={'file': (io.BytesIO(b'Q1. Age?'), 'survey.txt')},
                     content_type='multipart/form-data',
                     headers={'X-CSRF-Token': token})
    assert up.status_code == 200
    post_json(client, '/extract_pdf_codebook', {
        'filename': up.get_json()['filename'], 'mode': 'survey',
        'headers': [BIG] * 5 + [f'c{i}' for i in range(5000)]})
    sizes = _prompt_chars(fake_llm, 'survey_extract')
    assert sizes and max(sizes) < 100_000, sizes
