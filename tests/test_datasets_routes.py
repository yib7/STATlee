"""Dataset routes: upload/normalize (5.1), paging (5.8), cached codebook &
suggestions (5.6/4.5), conversational wrangling + version control (5.16),
project export (5.3), reset (4.6)."""
import io
import zipfile

from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv


def test_upload_accepts_csv(client):
    resp = upload_csv(client, SAMPLE_CSV)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['filename'] == 'test.csv'
    assert 'age' in body['profile']['headers']
    assert body['sha256']


def test_upload_as_logged_in_user_succeeds_without_dataset_model(client):
    """The write-only Dataset table was removed (P2-7): a logged-in user's
    upload must still succeed, and no such model should exist anymore."""
    post_json(client, '/register', {'email': 'uploader@x.com', 'password': 'longenough1'})
    resp = upload_csv(client, SAMPLE_CSV)
    assert resp.status_code == 200
    assert resp.get_json()['filename'] == 'test.csv'

    import statlee.models as models_mod
    assert not hasattr(models_mod, 'Dataset')


def test_upload_returns_initial_changelog(client):
    # The data-cleaning panel needs the v1 changelog to render on first upload.
    body = upload_csv(client, SAMPLE_CSV).get_json()
    cl = body['changelog']
    assert cl['active'] == 1
    assert cl['versions'][0]['instruction'] == 'Original upload'
    assert cl['can_undo'] is False


def test_upload_rejects_unsupported_format(client):
    token = csrf_token(client)
    data = {'file': (io.BytesIO(b'nope'), 'evil.exe')}
    resp = client.post('/upload', data=data,
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})
    assert resp.status_code == 400
    assert 'Supported' in resp.get_json()['error']


def test_upload_tsv_is_normalized(client):
    token = csrf_token(client)
    tsv = "a\tb\n1\t2\n3\t4\n"
    data = {'file': (io.BytesIO(tsv.encode()), 'd.tsv')}
    resp = client.post('/upload', data=data,
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})
    assert resp.status_code == 200
    assert resp.get_json()['filename'] == 'd.csv'   # converted to canonical CSV


def test_data_page_pagination(client):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/data_page',
                     {'filename': 'test.csv', 'per_page': 2, 'page': 1})
    body = resp.get_json()
    assert body['status'] == 'success'
    assert body['total_rows'] == 4
    assert body['total_pages'] == 2
    assert len(body['data']) == 2


def test_data_page_filter(client):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/data_page',
                     {'filename': 'test.csv', 'filters': {'group': 'B'}})
    body = resp.get_json()
    assert body['total_rows'] == 2
    assert all(r['group'] == 'B' for r in body['data'])


def test_data_page_rejects_non_integer_page(client):
    """P1-5: malformed 'page' must return a structured 400, not a 500."""
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/data_page',
                     {'filename': 'test.csv', 'page': 'abc'})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_data_page_rejects_non_integer_per_page(client):
    """P1-5: malformed 'per_page' must return a structured 400, not a 500."""
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/data_page',
                     {'filename': 'test.csv', 'per_page': 'x'})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_data_page_rejects_non_dict_filters(client):
    """P1-5: a list (or any non-dict) 'filters' must return a structured 400."""
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/data_page',
                     {'filename': 'test.csv', 'filters': ['a', 'b']})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_classify_variables_uses_llm_and_caches(client, fake_llm):
    fake_llm.set('classify', '{"age": "Continuous", "group": "Nominal"}')
    upload_csv(client, SAMPLE_CSV)

    first = post_json(client, '/classify_variables', {'filename': 'test.csv'})
    body = first.get_json()
    assert body['status'] == 'success'
    assert body['codebook']['age'] == 'Continuous'

    # Second call hits the per-hash cache (5.6) — no extra classify LLM call.
    calls_before = sum(1 for c in fake_llm.calls if c[1] == 'classify')
    second = post_json(client, '/classify_variables', {'filename': 'test.csv'})
    assert second.get_json().get('cached') is True
    calls_after = sum(1 for c in fake_llm.calls if c[1] == 'classify')
    assert calls_after == calls_before


def test_suggest_returns_three_and_reroll_bypasses_cache(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    first = post_json(client, '/suggest', {'filename': 'test.csv'})
    assert len(first.get_json()['suggestions']) == 3

    # Reroll (previous provided) must not be served from cache (4.5).
    before = sum(1 for c in fake_llm.calls if c[1] == 'suggest')
    rerolled = post_json(client, '/suggest',
                         {'filename': 'test.csv', 'previous': ['Suggestion 1']})
    assert rerolled.get_json().get('cached') is not True
    after = sum(1 for c in fake_llm.calls if c[1] == 'suggest')
    assert after == before + 1


def test_suggest_usage_includes_per_model_breakdown(client, fake_llm):
    # The session-cost tooltip needs a per-model split, not just totals.
    # Use the reroll path (previous provided) so it bypasses the shared cache
    # and actually makes a billable call that reports usage.
    upload_csv(client, SAMPLE_CSV)
    body = post_json(client, '/suggest',
                     {'filename': 'test.csv', 'previous': ['Suggestion 1']}).get_json()
    usage = body['usage']
    assert 'by_model' in usage
    assert usage['by_model']                      # at least one model
    assert usage['calls'] >= 1


def test_wrangle_response_reports_usage(client, fake_llm):
    # Wrangling is a billable call too; its cost must reach the client.
    upload_csv(client, SAMPLE_CSV)
    body = post_json(client, '/wrangle',
                     {'filename': 'test.csv', 'instruction': 'drop missing rows'}).get_json()
    assert 'usage' in body
    assert 'by_model' in body['usage']
    assert body['usage']['calls'] >= 1


def test_wrangle_creates_new_version(client, fake_llm):
    # Fake wrangle plan drops missing rows; SAMPLE_CSV has 2 rows with NaNs.
    fake_llm.set('wrangle', '{"code": "df = df.dropna()", '
                            '"summary": "Dropped missing rows", "error": null}')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/wrangle',
                     {'filename': 'test.csv', 'instruction': 'drop missing rows'})
    body = resp.get_json()
    assert body['status'] == 'success'
    assert body['changelog']['active'] == 2
    assert body['changelog']['can_undo'] is True

    # The active version now reflects the transform (NaN rows removed).
    page = post_json(client, '/data_page', {'filename': 'test.csv'}).get_json()
    assert page['total_rows'] == 2


def test_wrangle_uses_configured_lite_role(client, fake_llm):
    # Conversational cleaning should run on the cheap flash-lite tier (default).
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle',
              {'filename': 'test.csv', 'instruction': 'drop missing rows'})
    wrangle_calls = [c for c in fake_llm.calls if c[1] == 'wrangle']
    assert wrangle_calls, 'expected a wrangle LLM call'
    assert all(role == 'lite' for role, *_ in wrangle_calls)


def test_wrangle_passes_configured_output_limit(client, fake_llm, monkeypatch):
    # /wrangle must honor exec_output_limit like /run does (P2-6), instead of
    # falling back to run_in_sandbox's own default.
    from statlee import sandbox as sandbox_mod

    captured = {}
    real_run_in_sandbox = sandbox_mod.run_in_sandbox

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run_in_sandbox(*args, **kwargs)

    monkeypatch.setattr(sandbox_mod, 'run_in_sandbox', spy)
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle',
              {'filename': 'test.csv', 'instruction': 'drop missing rows'})
    assert captured.get('output_limit') == 256 * 1024


def test_revert_dataset_restores_original(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)                       # v1: 4 rows
    post_json(client, '/wrangle',
              {'filename': 'test.csv', 'instruction': 'drop missing rows'})  # v2: 2 rows
    resp = post_json(client, '/revert_dataset', {'filename': 'test.csv'})
    body = resp.get_json()
    assert body['status'] == 'success'
    assert body['changelog']['active'] == 3             # revert is a new version
    assert body['changelog']['can_undo'] is True        # ...and undo-able
    # Original four rows are back.
    page = post_json(client, '/data_page', {'filename': 'test.csv'}).get_json()
    assert page['total_rows'] == 4


def test_revert_dataset_is_undoable(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle',
              {'filename': 'test.csv', 'instruction': 'drop missing rows'})  # v2: 2 rows
    post_json(client, '/revert_dataset', {'filename': 'test.csv'})           # v3: 4 rows
    undo = post_json(client, '/version_control',
                     {'filename': 'test.csv', 'direction': 'undo'})
    assert undo.get_json()['changelog']['active'] == 2  # back to the wrangled state
    page = post_json(client, '/data_page', {'filename': 'test.csv'}).get_json()
    assert page['total_rows'] == 2


def test_revert_unknown_dataset_404(client):
    resp = post_json(client, '/revert_dataset', {'filename': 'nope.csv'})
    assert resp.status_code == 404


def test_wrangle_blocked_by_moderation(client, fake_llm):
    fake_llm.block('Off-topic')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/wrangle',
                     {'filename': 'test.csv', 'instruction': 'write a poem'})
    assert resp.status_code == 403


def test_version_control_undo_redo_over_http(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle',
              {'filename': 'test.csv', 'instruction': 'drop missing rows'})

    undo = post_json(client, '/version_control',
                     {'filename': 'test.csv', 'direction': 'undo'})
    assert undo.get_json()['changelog']['active'] == 1

    redo = post_json(client, '/version_control',
                     {'filename': 'test.csv', 'direction': 'redo'})
    assert redo.get_json()['changelog']['active'] == 2


def test_version_control_validates_direction(client):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/version_control',
                     {'filename': 'test.csv', 'direction': 'sideways'})
    assert resp.status_code == 400


def test_export_bundles_zip(client):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/export', {
        'filename': 'test.csv', 'language': 'Python',
        'code': "print('hi')",
        'history': [{'role': 'user', 'text': 'analyze it'}],
        'interpretation': 'It works.',
    })
    assert resp.status_code == 200
    assert resp.mimetype == 'application/zip'
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert 'script.py' in names
    assert 'report.md' in names
    assert any(n.startswith('data/') for n in names)


def test_export_skips_non_dict_history_items(client):
    """P1-5: a malformed (non-dict) history item must not 500 the export."""
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/export', {
        'filename': 'test.csv', 'language': 'Python',
        'code': "print('hi')",
        'history': ['oops', {'role': 'user', 'text': 'analyze it'}],
        'interpretation': 'It works.',
    })
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    report_text = zf.read('report.md').decode('utf-8')
    assert 'analyze it' in report_text


def test_reset_clears_workspace(client):
    upload_csv(client, SAMPLE_CSV)
    assert post_json(client, '/reset', {}).get_json()['status'] == 'success'
    # After reset the dataset is gone.
    resp = post_json(client, '/data_page', {'filename': 'test.csv'})
    assert resp.status_code == 400


def test_extract_pdf_codebook_survey_mode(client, fake_llm):
    """Survey→codebook branch (5.13): a TXT 'survey' yields an inferred map."""
    fake_llm.set('survey_extract', '{"age": "Q1 age in years"}')
    token = csrf_token(client)
    # Upload a TXT which the route converts to a PDF artifact.
    data = {'file': (io.BytesIO(b'Q1. What is your age?'), 'survey.txt')}
    up = client.post('/upload_pdf', data=data,
                     content_type='multipart/form-data',
                     headers={'X-CSRF-Token': token})
    assert up.status_code == 200
    pdf_name = up.get_json()['filename']

    resp = post_json(client, '/extract_pdf_codebook',
                     {'filename': pdf_name, 'mode': 'survey', 'headers': ['age']})
    body = resp.get_json()
    assert body['status'] == 'success'
    assert body['mapping']['age'].startswith('Q1')
