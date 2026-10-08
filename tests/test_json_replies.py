"""Tolerant JSON extraction and per-route shape validation for every JSON-mode
model call (deep audit D3-9, D3-10, D4-21)."""
import json

import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import llm
from statlee.routes import moderation_blocked, parse_json_reply

# ---------------------------------------------------------------------------
# D3-10: the shared extractor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('text', [
    '{"decision": "pass"}',
    '```json\n{"decision": "pass"}\n```',
    '```\n{"decision": "pass"}\n```',
    'Here is the verdict:\n{"decision": "pass"}',
    '{"decision": "pass"}\nThat is my answer.',
])
def test_parse_json_reply_tolerates_fences_and_prose(text):
    assert parse_json_reply(text) == {'decision': 'pass'}


def test_parse_json_reply_finds_an_array():
    assert parse_json_reply('Sure:\n["a", "b"]') == ['a', 'b']


@pytest.mark.parametrize('text', ['', 'no json here', '```json\n```', None])
def test_parse_json_reply_rejects_garbage(text):
    with pytest.raises(llm.MalformedReplyError):
        parse_json_reply(text)


def test_fenced_moderation_pass_is_honored():
    blocked, _ = moderation_blocked(
        '```json\n{"decision": "pass", "reason": ""}\n```')
    assert blocked is False


def test_moderation_still_fails_closed_on_garbage():
    assert moderation_blocked('I think it is fine')[0] is True
    assert moderation_blocked('```json\n{"decision": "maybe"}\n```')[0] is True


def test_fenced_moderation_reply_does_not_403_chat(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('moderation', '```json\n{"decision": "pass"}\n```')
    fake_llm.set('code_moderation', 'Verdict: {"decision": "pass"}')
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'summarize'})
    assert resp.status_code == 200
    assert any(e.get('type') == 'done' for e in sse_events(resp))


# ---------------------------------------------------------------------------
# D3-9 / D4-21: /suggest always returns a list of at most 3 strings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('reply', [
    json.dumps({'suggestions': ['Run A.', 'Run B.', 'Run C.']}),
    json.dumps(['Run A.', 'Run B.', 'Run C.', 'Run D.']),
    '```json\n' + json.dumps({'ideas': ['Run A.', 'Run B.', 'Run C.']}) + '\n```',
    json.dumps(['Run A.', 7, None, {'x': 1}, 'Run B.', '  ', 'Run C.']),
])
def test_suggest_normalizes_to_three_strings(client, fake_llm, reply):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('suggest', reply)
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert resp.status_code == 200
    sugg = resp.get_json()['suggestions']
    assert isinstance(sugg, list) and 1 <= len(sugg) <= 3
    assert all(isinstance(s, str) and s.strip() for s in sugg)


def test_suggest_prompt_asks_for_an_object(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/suggest', {'filename': 'test.csv'})
    prompt = next(t for _, k, t in fake_llm.calls if k == 'suggest')
    assert '"suggestions"' in prompt


def test_suggest_unusable_reply_is_502_and_not_cached(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('suggest', json.dumps({'note': 'no ideas'}))
    resp = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert resp.status_code == 502
    fake_llm.set('suggest', json.dumps(['Run A.']))
    again = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert again.status_code == 200 and again.get_json()['suggestions'] == ['Run A.']
    assert not again.get_json().get('cached')


# ---------------------------------------------------------------------------
# D3-9: classify / method_prompt / pdf extraction / wrangle / feature selection
# ---------------------------------------------------------------------------
def test_classify_drops_invalid_labels_and_normalizes_case(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('classify', 'Here you go:\n' + json.dumps(
        {'age': 'continuous', 'income': {'x': 1}, 'group': 'Nominal',
         'nope': 'Nominal'}))
    resp = post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert resp.status_code == 200
    assert resp.get_json()['codebook'] == {'age': 'Continuous',
                                          'group': 'Nominal'}


def test_classify_non_object_reply_is_502(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('classify', json.dumps(['Continuous']))
    resp = post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert resp.status_code == 502


def test_method_prompt_without_prompt_is_502(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('method_prompt', json.dumps({'rationale': 'x'}))
    resp = post_json(client, '/method_prompt', {
        'filename': 'test.csv', 'method': {'name': 'OLS'}})
    assert resp.status_code == 502


def test_method_prompt_fenced_reply_is_accepted(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('method_prompt', '```json\n' + json.dumps(
        {'prompt': 'Run OLS of income on age.', 'rationale': 7}) + '\n```')
    resp = post_json(client, '/method_prompt', {
        'filename': 'test.csv', 'method': {'name': 'OLS'}})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['prompt'] == 'Run OLS of income on age.'
    assert isinstance(body['rationale'], str)


def _upload_txt_codebook(client):
    import io

    from conftest import csrf_token
    token = csrf_token(client)
    return client.post('/upload_pdf', data={
        'file': (io.BytesIO(b'age: age in years'), 'book.txt')},
        content_type='multipart/form-data', headers={'X-CSRF-Token': token})


def test_pdf_extract_mapping_values_are_strings(client, fake_llm):
    _upload_txt_codebook(client)
    fake_llm.set('pdf_extract', json.dumps(
        {'age': 'Age in years', 'sex': {'1': 'Male', '2': 'Female'},
         'n': 3, 'gone': None}))
    resp = post_json(client, '/extract_pdf_codebook', {'filename': 'book.txt'})
    assert resp.status_code == 200
    mapping = resp.get_json()['mapping']
    assert mapping['age'] == 'Age in years'
    assert all(isinstance(v, str) for v in mapping.values())
    assert 'gone' not in mapping and 'Male' in mapping['sex']


def test_pdf_extract_non_object_is_502(client, fake_llm):
    _upload_txt_codebook(client)
    fake_llm.set('pdf_extract', json.dumps(['age']))
    resp = post_json(client, '/extract_pdf_codebook', {'filename': 'book.txt'})
    assert resp.status_code == 502


def test_wrangle_unparsable_plan_is_502(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('wrangle', 'I would drop the column.')
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop group'})
    assert resp.status_code == 502


def test_wrangle_fenced_plan_is_accepted(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('wrangle', '```json\n' + json.dumps({
        'code': "df = df.drop(columns=['group'])", 'summary': 'Dropped group',
        'error': None}) + '\n```')
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop group'})
    assert resp.status_code == 200, resp.get_json()


def test_wrangle_non_string_error_is_502(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('wrangle', json.dumps({'code': None, 'error': {'x': 1}}))
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop group'})
    assert resp.status_code == 502


def test_feature_selection_fenced_reply_is_used(client, fake_llm):
    cols = [f'c{i}' for i in range(20)]
    csv = ','.join(cols) + '\n' + ','.join('1' for _ in cols) + '\n'
    upload_csv(client, csv, 'wide.csv')
    fake_llm.set('feature_selection', '```json\n' + json.dumps(
        {'required_columns': ['c3', 'c7']}) + '\n```')
    events = sse_events(post_json(client, '/chat', {
        'filename': 'wide.csv', 'prompt': 'compare c3 and c7'}))
    assert not any(e.get('phase') == 'feature_selection_skipped'
                   for e in events)
    draft = next(t for _, k, t in fake_llm.calls if k == 'draft')
    assert 'c3' in draft and "'c12'" not in draft and 'c12,' not in draft
